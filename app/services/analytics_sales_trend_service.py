# app/services/analytics_sales_trend_service.py
# -*- coding: utf-8 -*-
# 카테고리
#    분석/KPI
# 작업선택
#    품목별 매출 추세 분석
#    품목별 매출 예상
#    품목별 재고부족현황
# backup (pre_enter_to_submit_false_master_analytics_20260505_223546) 재고수량 구하는 방법변경
from __future__ import annotations

import calendar
import logging
import time
from typing import Any, Dict, Optional

import pandas as pd

from app.services.rddbc_io_common import (
    build_result_payload,
    clean_text,
    coalesce_params,
    like_value,
    query_to_df,
)

TABLE = "analytics_sales_trend"
log = logging.getLogger("ssai.sims.analytics_sales_trend")

SOURCE_LABELS = {
    "monthly_book": "월집계-장부재고(Rddbc220)",
    "monthly_real": "월집계-실재고(Rddbc210)",
    "detail": "출고상세(Rddbc120)",
    "auto": "자동",
}

STOCK_MODE_LABELS = {
    "book": "장부재고",
    "real": "실재고",
}

SALES_TREND_PUBLIC_COLUMNS = [
    "기준월",
    "제품코드",
    "제품명",
    "규격",
    "제조사코드",
    "제조사명",
    "제품그룹Gcode",
    "제품그룹코드",
    "제품그룹명",
    "제품구분Gcode",
    "제품구분코드",
    "제품구분명",
    "제품분류Gcode",
    "제품분류코드",
    "제품분류명",
    "매입처코드",
    "매입처명",
    "재고적용처코드",
    "재고적용처명",
    "출고수량",
    "출고할증수량",
    "매출공급가액",
    "매출세액",
    "매출합계",
    "집계건수",
    "매입처수",
    "분석자료원",
    "기간구분",
    "전월대비수량",
    "전월대비매출",
    "최근3개월평균매출",
    "최근6개월평균매출",
    "월시점 완료월수",
    "월시점 완료월평균매출",
    "월시점 최근3개월평균매출",
    "월시점 최근6개월평균매출",
    "월시점 증감률",
    "월시점 추세판정",
    "월시점 판정결과",
    "월시점 실제매출",
    "월시점 예상기준",
    "월시점 적용증감률",
    "월시점 예상매출",
    "월시점 예상대비차이",
    "월시점 잔여예상",
    "월시점 달성률",
    "추세판정",
    "판정결과",
]


def _source_label(source_mode: Any) -> str:
    key = str(source_mode or "").strip()
    return SOURCE_LABELS.get(key, key)


def _effective_source_label(source_mode: Any, df: Any = None) -> str:
    if isinstance(df, pd.DataFrame) and bool(df.attrs.get("mixed_current_month_detail")):
        completed = str(df.attrs.get("source_label_completed") or _source_label(source_mode))
        current = str(df.attrs.get("source_label_current") or "출고상세(Rddbc120)")
        return f"완료월: {completed} / 당월: {current}"
    return _source_label(source_mode)


def _finalize_sales_trend_public_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

    attrs = dict(getattr(df, "attrs", {}) or {})
    out = df.copy()
    for col in SALES_TREND_PUBLIC_COLUMNS:
        if col not in out.columns:
            out[col] = ""
    out = out[SALES_TREND_PUBLIC_COLUMNS].copy()
    out.attrs.update(attrs)
    return out


def _sum_numeric(df: pd.DataFrame, col: str) -> float:
    if df is None or df.empty or col not in df.columns:
        return 0.0
    return float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())


def _normalize_analytics_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

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
        "평균매출",
        "현재매출",
        "예상매출",
        "잔여예상",
        "진척률",
        "증감률",
        "달성률",
        "완료월평균매출",
        "완료월총매출",
        "실제매출",
        "예상대비차이",
        "완료월수",
    )

    out = df.copy()
    for col in out.columns:
        s = str(col or "")
        s_lower = s.lower()
        if any(w in s or w in s_lower for w in code_words):
            out[col] = out[col].map(lambda v: "" if pd.isna(v) else str(v).strip())
            continue
        if any(w in s for w in numeric_words):
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
    return out

def _ensure_analysis_seq_column(
    df: pd.DataFrame,
    *,
    mode: str = "row",
    product_key_col: str = "제품코드",
) -> pd.DataFrame:
    """
    분석/KPI 결과용 순번 컬럼 보정.

    mode="row"
      - 1행 단위로 1, 2, 3...
      - 요약표/예상/재고부족현황에 적합

    mode="product"
      - 같은 제품코드는 같은 순번
      - 품목별 매출 추세 분석처럼 제품별 월별 행이 여러 줄인 표에 적합
    """
    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            return pd.DataFrame()

    if df.empty:
        return df

    out = df.copy()

    if "순번" in out.columns:
        out = out.drop(columns=["순번"])

    if mode == "product":
        if product_key_col in out.columns:
            key_series = out[product_key_col].fillna("").astype(str).str.strip()
        elif "제품명" in out.columns:
            key_series = out["제품명"].fillna("").astype(str).str.strip()
        else:
            key_series = pd.Series([str(i) for i in range(len(out))], index=out.index)

        seq_map: dict[str, int] = {}
        seq_values: list[int] = []

        for key in key_series:
            key = str(key or "").strip()
            if not key:
                key = f"__row_{len(seq_values)}"

            if key not in seq_map:
                seq_map[key] = len(seq_map) + 1

            seq_values.append(seq_map[key])

        out.insert(0, "순번", seq_values)
    else:
        out.insert(0, "순번", range(1, len(out) + 1))

    return out

def _trend_counts_from_trend_df(df: pd.DataFrame) -> Dict[str, int]:
    """
    품목별 매출 추세 분석 원자료에서 추세판정별 제품 수를 계산한다.

    주의:
    - 추세 분석은 제품별 월별 행이 여러 줄일 수 있다.
    - 따라서 단순 행 수가 아니라 제품코드 기준 중복 제거 후 계산한다.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return {}

    if "추세판정" not in df.columns:
        return {}

    work = df.copy()

    if "제품코드" in work.columns:
        work["_제품키"] = work["제품코드"].fillna("").astype(str).str.strip()
    elif "제품명" in work.columns:
        work["_제품키"] = work["제품명"].fillna("").astype(str).str.strip()
    else:
        work["_제품키"] = work.index.astype(str)

    work["_판정"] = work["추세판정"].fillna("").astype(str).str.strip()
    work = work[(work["_제품키"] != "") & (work["_판정"] != "")]

    if work.empty:
        return {}

    # 제품별 1건만 남긴 뒤 판정 집계
    work = work.drop_duplicates(subset=["_제품키"], keep="first")

    counts = work["_판정"].value_counts(dropna=True).to_dict()
    return {str(k): int(v) for k, v in counts.items()}


# 텍스트값 빈도 계산. NaN/None/공백은 empty_label로 통합.
# 예: {'A': 10, 'B': 5, '미분류': 3}
# 분석기간/제품코드/제품명/제조사명/매입처명/재고적용처명 등 텍스트값 분포 확인용으로 사용.
def _count_text_values(df: pd.DataFrame, col: str, empty_label: str = "미분류") -> Dict[str, int]:
    if df is None or df.empty or col not in df.columns:
        return {}

    vc = (
        df[col]
        .fillna("")
        .astype(str)
        .str.strip()
        .replace("", empty_label)
        .value_counts()
    )
    return {str(k): int(v) for k, v in vc.items()}

def _digits_only(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _last_day_yyyymm(yyyymm: str) -> str:
    y = int(yyyymm[:4])
    m = int(yyyymm[4:6])
    last = calendar.monthrange(y, m)[1]
    return f"{yyyymm}{last:02d}"


def _prev_yyyymm(yyyymm: str) -> str:
    month = _normalize_month(yyyymm)
    if not month:
        return ""
    y = int(month[:4])
    m = int(month[4:6])
    if m == 1:
        return f"{y - 1}12"
    return f"{y}{m - 1:02d}"


def _is_mid_month_date(date_value: Any) -> bool:
    date_to = _normalize_date_to(date_value)
    if not date_to:
        return False
    return date_to != _last_day_yyyymm(date_to[:6])


def _policy_today_yyyymmdd(params: Optional[Dict[str, Any]] = None) -> str:
    params = params or {}
    for key in ("policy_date", "as_of_date", "today"):
        value = _normalize_date_to(params.get(key))
        if value:
            return value
    try:
        return pd.Timestamp.today().strftime("%Y%m%d")
    except Exception:
        return dt.datetime.now().strftime("%Y%m%d")


def _resolve_period_source_policy(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = params or {}
    today = _policy_today_yyyymmdd(params)
    requested_date_to = _normalize_date_to(params.get("date_to")) or today
    effective_date_to = min(requested_date_to, today)
    effective_month_to = effective_date_to[:6]
    is_past = requested_date_to < today
    is_month_end = effective_date_to == _last_day_yyyymm(effective_month_to)
    use_hybrid = bool(is_past and not is_month_end)
    if not is_past:
        evaluation_mode = "current_monthly"
    elif is_month_end:
        evaluation_mode = "historical_month_end"
    else:
        evaluation_mode = "historical_midmonth"
    month_from = _normalize_month(params.get("month_from"))
    basis_months = [
        m for m in _iter_yyyymm(month_from, effective_month_to)
        if m and m < effective_month_to
    ] if month_from and effective_month_to else []
    return {
        "today": today,
        "requested_date_to": requested_date_to,
        "effective_date_to": effective_date_to,
        "effective_month_to": effective_month_to,
        "evaluation_month": effective_month_to,
        "basis_months": basis_months,
        "evaluation_mode": evaluation_mode,
        "is_month_end": is_month_end,
        "use_hybrid": use_hybrid,
        "use_hybrid_detail": use_hybrid,
        "use_monthly_only": not use_hybrid,
    }


def _apply_period_source_policy_params(params: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(params or {})
    policy = _resolve_period_source_policy(out)
    out["date_to"] = policy["effective_date_to"]
    out["month_to"] = policy["effective_month_to"]
    out["_period_source_policy"] = policy
    return out


def _normalize_month(value: Any) -> str:
    s = _digits_only(value)
    if len(s) >= 6:
        return s[:6]
    return ""


def _normalize_date_from(value: Any) -> str:
    s = _digits_only(value)
    if len(s) >= 8:
        return s[:8]
    if len(s) == 6:
        return s + "01"
    return ""


def _normalize_date_to(value: Any) -> str:
    s = _digits_only(value)
    if len(s) >= 8:
        return s[:8]
    if len(s) == 6:
        return _last_day_yyyymm(s)
    return ""


def _apply_month_or_date_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    분석 기간 보정.
    우선순위:
    1. date_from/date_to
    2. month_from/month_to
    3. 없으면 현재 연도 1월 ~ 현재월은 view/NLQ 쪽에서 기본값 부여 권장
    """
    out = dict(params or {})

    date_from = _normalize_date_from(out.get("date_from"))
    date_to = _normalize_date_to(out.get("date_to"))

    month_from = _normalize_month(out.get("month_from"))
    month_to = _normalize_month(out.get("month_to"))

    if not date_from and month_from:
        date_from = month_from + "01"
    if not date_to and month_to:
        date_to = _last_day_yyyymm(month_to)

    if date_from:
        out["date_from"] = date_from
    if date_to:
        out["date_to"] = date_to

    return out


def _add_filter(clauses: list[str], condition: str) -> None:
    if condition and condition.strip():
        clauses.append(condition.strip())


def _clean_list_param(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        return []
    return [str(v).strip() for v in raw_values if str(v).strip()]


def _add_in_filter(
    clauses: list[str],
    params: Dict[str, Any],
    field_sql: str,
    key_prefix: str,
    values: list[str],
) -> bool:
    clean_values = _clean_list_param(values)
    if not clean_values:
        return False

    names: list[str] = []
    for i, value in enumerate(clean_values):
        key = f"{key_prefix}_in_{i}"
        params[key] = value
        names.append(f"%({key})s")
    _add_filter(clauses, f"{field_sql} IN ({', '.join(names)})")
    return True


def _build_filters(params: Dict[str, Any]) -> str:
    clauses: list[str] = []

    # 기간
    if clean_text(params.get("date_from")):
        _add_filter(clauses, "Out_Put.Rd12_Out_YyMmDd >= %(date_from)s")
    if clean_text(params.get("date_to")):
        _add_filter(clauses, "Out_Put.Rd12_Out_YyMmDd <= %(date_to)s")

    # 매출 추세 1차 기본: 정상출고 + 출고반품
    # 5xx = 정상출고(+), 6xx = 출고반품(-)
    sales_prefixes = params.get("sales_prefixes")
    if isinstance(sales_prefixes, (list, tuple, set)):
        vals = [str(x).strip() for x in sales_prefixes if str(x).strip()]
    else:
        vals = []

    if vals:
        names = []
        for i, v in enumerate(vals):
            key = f"sales_prefix_{i}"
            params[key] = v[:1]
            names.append(f"%({key})s")
        _add_filter(clauses, f"LEFT(Out_Put.Rd12_Io_Gu, 1) IN ({', '.join(names)})")
    else:
        _add_filter(clauses, "LEFT(Out_Put.Rd12_Io_Gu, 1) IN ('5', '6')")

    # 제품
    if clean_text(params.get("physic_cd")):
        _add_filter(clauses, "Out_Put.Rd12_Physic_Cd = %(physic_cd)s")

    if like_value(params.get("physic_nm")):
        params["physic_nm_like"] = like_value(params.get("physic_nm"))
        _add_filter(clauses, "Physic_Cd.Rd04_Physic_Nm LIKE %(physic_nm_like)s")

    # 거래처
    if clean_text(params.get("ven_cd")):
        _add_filter(clauses, "Out_Put.Rd12_Ven_Cd = %(ven_cd)s")

    if like_value(params.get("ven_nm")):
        params["ven_nm_like"] = like_value(params.get("ven_nm"))
        _add_filter(clauses, "Ven_Cd.Rd03_Ven_Nm LIKE %(ven_nm_like)s")

    # 매입처
    if clean_text(params.get("buy_cd")):
        _add_filter(clauses, "Out_Put.Rd12_In_Ven_Cd = %(buy_cd)s")

    if like_value(params.get("buy_nm")):
        params["buy_nm_like"] = like_value(params.get("buy_nm"))
        _add_filter(clauses, "In_Ven_Cd.Rd03_Ven_Nm LIKE %(buy_nm_like)s")

    # 실납처
    if clean_text(params.get("real_ven_cd")):
        _add_filter(clauses, "Out_Put.Rd12_Real_Ven_Cd = %(real_ven_cd)s")

    if like_value(params.get("real_ven_nm")):
        params["real_ven_nm_like"] = like_value(params.get("real_ven_nm"))
        _add_filter(clauses, "Real_Ven_Cd.Rd03_Ven_Nm LIKE %(real_ven_nm_like)s")

    # 제조사 / 제약사
    if clean_text(params.get("product_ven_cd")):
        _add_filter(clauses, "Physic_Cd.Rd04_Ven_Cd = %(product_ven_cd)s")

    if like_value(params.get("product_ven_nm")):
        params["product_ven_nm_like"] = like_value(params.get("product_ven_nm"))
        _add_filter(clauses, "Make_Ven.Rd03_Ven_Nm LIKE %(product_ven_nm_like)s")

    # 제품그룹 / 제품구분 / 제품분류
    # 코드값이 있으면 코드로 우선 필터링하고, 명칭은 보조 LIKE 조건으로 사용한다.
    if clean_text(params.get("product_group")):
        _add_filter(clauses, "Physic_Cd.Rd04_Physic_Group = %(product_group)s")
    elif like_value(params.get("product_group_nm")):
        params["product_group_nm_like"] = like_value(params.get("product_group_nm"))
        _add_filter(clauses, "Physic_Group_Nm.Rd01_Hnm LIKE %(product_group_nm_like)s")

    if _add_in_filter(
        clauses,
        params,
        "Physic_Cd.Rd04_Physic_Di",
        "product_di",
        _clean_list_param(params.get("product_di_list")),
    ):
        pass
    elif clean_text(params.get("product_di")):
        _add_filter(clauses, "Physic_Cd.Rd04_Physic_Di = %(product_di)s")
    elif like_value(params.get("product_di_nm")):
        params["product_di_nm_like"] = like_value(params.get("product_di_nm"))
        _add_filter(clauses, "Physic_Di_Nm.Rd01_Hnm LIKE %(product_di_nm_like)s")

    if _add_in_filter(
        clauses,
        params,
        "Physic_Cd.Rd04_Physic_Gu",
        "product_class",
        _clean_list_param(params.get("product_class_list")),
    ):
        pass
    elif clean_text(params.get("product_class")):
        _add_filter(clauses, "Physic_Cd.Rd04_Physic_Gu = %(product_class)s")
    elif like_value(params.get("product_class_nm")):
        params["product_class_nm_like"] = like_value(params.get("product_class_nm"))
        _add_filter(clauses, "Physic_Gu_Nm.Rd01_Hnm LIKE %(product_class_nm_like)s")


    # 재고위치
    if _add_in_filter(
        clauses,
        params,
        "Out_Put.Rd12_Stock_Cd",
        "stock_cd",
        _clean_list_param(params.get("stock_cd_list")),
    ):
        pass
    elif clean_text(params.get("stock_cd")):
        _add_filter(clauses, "Out_Put.Rd12_Stock_Cd = %(stock_cd)s")

    if like_value(params.get("stock_nm")):
        params["stock_nm_like"] = like_value(params.get("stock_nm"))
        _add_filter(clauses, "Stock_Cd.Rd01_Hnm LIKE %(stock_nm_like)s")

    # 영업사원
    if clean_text(params.get("sales_man")):
        _add_filter(clauses, "Out_Put.Rd12_Sales_Man = %(sales_man)s")

    if like_value(params.get("sales_man_nm")):
        params["sales_man_nm_like"] = like_value(params.get("sales_man_nm"))
        _add_filter(clauses, "Sales_Man.Rd06_User_Nm LIKE %(sales_man_nm_like)s")

    # 지역 조건: 거래처 도로명주소 기준
    if like_value(params.get("sido_nm")):
        params["sido_nm_like"] = like_value(params.get("sido_nm"))
        _add_filter(clauses, "Road1.Rd021_Sido LIKE %(sido_nm_like)s")

    if like_value(params.get("gugun_nm")):
        params["gugun_nm_like"] = like_value(params.get("gugun_nm"))
        _add_filter(clauses, "Road1.Rd021_Gugun LIKE %(gugun_nm_like)s")

    if like_value(params.get("dong_nm")):
        params["dong_nm_like"] = like_value(params.get("dong_nm"))
        _add_filter(clauses, "Road1.Rd021_DongNm LIKE %(dong_nm_like)s")

    if like_value(params.get("road_nm")):
        params["road_nm_like"] = like_value(params.get("road_nm"))
        _add_filter(clauses, "Road1.Rd021_RoadNm LIKE %(road_nm_like)s")

    if like_value(params.get("road_addr_kw")):
        params["road_addr_kw_like"] = like_value(params.get("road_addr_kw"))
        _add_filter(
            clauses,
            """(
                Road1.Rd021_Sido LIKE %(road_addr_kw_like)s
                OR Road1.Rd021_Gugun LIKE %(road_addr_kw_like)s
                OR Road1.Rd021_DongNm LIKE %(road_addr_kw_like)s
                OR Road1.Rd021_RoadNm LIKE %(road_addr_kw_like)s
                OR Ven_Cd.Rd03_Address LIKE %(road_addr_kw_like)s
                OR Ven_Cd.Rd03_Address2 LIKE %(road_addr_kw_like)s
            )""",
        )

    return ("\n      AND " + "\n      AND ".join(clauses)) if clauses else ""

def _needs_detail_source(params: Dict[str, Any]) -> bool:
    """
    월집계로 정확히 표현하기 어려운 조건이면 Rddbc120 상세로 보낸다.

    월집계의 Ven_Cd는 현재 매입처 scope에 가깝게 쓰므로,
    매출처/지역/영업사원/실납처 기준은 상세 테이블을 사용한다.
    """
    detail_keys = [
        "ven_cd",
        "ven_nm",
        "real_ven_cd",
        "real_ven_nm",
        "sales_man",
        "sales_man_nm",
        "sido_nm",
        "gugun_nm",
        "dong_nm",
        "road_nm",
        "road_addr_kw",
    ]
    return any(clean_text(params.get(k)) for k in detail_keys)


def _resolve_source_mode(params: Dict[str, Any]) -> str:
    mode = clean_text(params.get("source_mode")).lower()

    if mode in {"detail", "rddbc120", "출고상세", "상세"}:
        return "detail"

    if mode in {"monthly_real", "real", "rddbc210", "실재고", "월집계실재고"}:
        return "monthly_real"

    if mode in {"monthly_book", "book", "rddbc220", "장부재고", "월집계장부"}:
        return "monthly_book"

    # auto
    if _needs_detail_source(params):
        return "detail"

    return "monthly_book"


def _monthly_spec(source_mode: str) -> Dict[str, str]:
    if source_mode == "monthly_real":
        return {
            "table": "dbo.Rddbc210",
            "alias": "M",
            "prefix": "Rd21",
            "title": "월집계-실재고",
            "out_prefixes": "'5','6','8','9'",
        }

    return {
        "table": "dbo.Rddbc220",
        "alias": "M",
        "prefix": "Rd22",
        "title": "월집계-장부재고",
        "out_prefixes": "'5','6','7','9'",
    }

def _build_monthly_filters(params: Dict[str, Any], spec: Dict[str, str]) -> str:
    clauses: list[str] = []
    p = spec["prefix"]
    a = spec["alias"]

    if clean_text(params.get("month_from")):
        _add_filter(clauses, f"{a}.{p}_Stock_YyMm >= %(month_from)s")
    if clean_text(params.get("month_to")):
        _add_filter(clauses, f"{a}.{p}_Stock_YyMm <= %(month_to)s")

    # date_from/date_to만 넘어온 경우 YYYYMM으로 변환
    if not clean_text(params.get("month_from")) and clean_text(params.get("date_from")):
        params["month_from"] = _digits_only(params.get("date_from"))[:6]
        _add_filter(clauses, f"{a}.{p}_Stock_YyMm >= %(month_from)s")

    if not clean_text(params.get("month_to")) and clean_text(params.get("date_to")):
        params["month_to"] = _digits_only(params.get("date_to"))[:6]
        _add_filter(clauses, f"{a}.{p}_Stock_YyMm <= %(month_to)s")

    # 출고 계열만
    _add_filter(clauses, f"LEFT({a}.{p}_Io_Gu, 1) IN ({spec['out_prefixes']})")

    if clean_text(params.get("physic_cd")):
        _add_filter(clauses, f"{a}.{p}_Physic_Cd = %(physic_cd)s")

    if like_value(params.get("physic_nm")):
        params["physic_nm_like"] = like_value(params.get("physic_nm"))
        _add_filter(clauses, "Physic_Cd.Rd04_Physic_Nm LIKE %(physic_nm_like)s")

    if clean_text(params.get("product_ven_cd")):
        _add_filter(clauses, "Physic_Cd.Rd04_Ven_Cd = %(product_ven_cd)s")

    if like_value(params.get("product_ven_nm")):
        params["product_ven_nm_like"] = like_value(params.get("product_ven_nm"))
        _add_filter(clauses, "Make_Ven.Rd03_Ven_Nm LIKE %(product_ven_nm_like)s")
#   제품그룹 / 제품구분 / 제품분류는 코드값이 있으면 코드로 우선 필터링하고, 명칭은 보조 LIKE 조건으로 사용한다.

    if clean_text(params.get("product_group")):
        _add_filter(clauses, "Physic_Cd.Rd04_Physic_Group = %(product_group)s")
    elif like_value(params.get("product_group_nm")):
        params["product_group_nm_like"] = like_value(params.get("product_group_nm"))
        _add_filter(clauses, "Physic_Group_Nm.Rd01_Hnm LIKE %(product_group_nm_like)s")

    if _add_in_filter(
        clauses,
        params,
        "Physic_Cd.Rd04_Physic_Di",
        "product_di_monthly",
        _clean_list_param(params.get("product_di_list")),
    ):
        pass
    elif clean_text(params.get("product_di")):
        _add_filter(clauses, "Physic_Cd.Rd04_Physic_Di = %(product_di)s")
    elif like_value(params.get("product_di_nm")):
        params["product_di_nm_like"] = like_value(params.get("product_di_nm"))
        _add_filter(clauses, "Physic_Di_Nm.Rd01_Hnm LIKE %(product_di_nm_like)s")

    if _add_in_filter(
        clauses,
        params,
        "Physic_Cd.Rd04_Physic_Gu",
        "product_class_monthly",
        _clean_list_param(params.get("product_class_list")),
    ):
        pass
    elif clean_text(params.get("product_class")):
        _add_filter(clauses, "Physic_Cd.Rd04_Physic_Gu = %(product_class)s")
    elif like_value(params.get("product_class_nm")):
        params["product_class_nm_like"] = like_value(params.get("product_class_nm"))
        _add_filter(clauses, "Physic_Gu_Nm.Rd01_Hnm LIKE %(product_class_nm_like)s")

    if _add_in_filter(
        clauses,
        params,
        f"{a}.{p}_Stock_Cd",
        "stock_cd_monthly",
        _clean_list_param(params.get("stock_cd_list")),
    ):
        pass
    elif clean_text(params.get("stock_cd")):
        _add_filter(clauses, f"{a}.{p}_Stock_Cd = %(stock_cd)s")

    if like_value(params.get("stock_nm")):
        params["stock_nm_like"] = like_value(params.get("stock_nm"))
        _add_filter(clauses, "Stock_Cd.Rd01_Hnm LIKE %(stock_nm_like)s")

    # 월집계의 Ven_Cd는 매입처 scope로 사용
    if clean_text(params.get("buy_cd")):
        _add_filter(clauses, f"{a}.{p}_Ven_Cd = %(buy_cd)s")

    if like_value(params.get("buy_nm")):
        params["buy_nm_like"] = like_value(params.get("buy_nm"))
        _add_filter(clauses, "Buy_Ven.Rd03_Ven_Nm LIKE %(buy_nm_like)s")

    return ("\n      AND " + "\n      AND ".join(clauses)) if clauses else ""


_MONTHLY_FAST_MASTER_FILTER_KEYS = (
    "physic_nm",
    "product_ven_cd",
    "product_ven_nm",
    "product_group",
    "product_group_nm",
    "product_di",
    "product_di_nm",
    "product_di_list",
    "product_class",
    "product_class_nm",
    "product_class_list",
    "buy_nm",
    "ven_nm",
    "real_ven_nm",
)


def _monthly_fast_path_reason(params: Dict[str, Any], source_mode: str) -> str:
    if source_mode not in {"monthly_book", "monthly_real"}:
        return "source_mode"

    if not clean_text(params.get("month_from")) and not clean_text(params.get("date_from")):
        return "missing_month_from"
    if not clean_text(params.get("month_to")) and not clean_text(params.get("date_to")):
        return "missing_month_to"

    for key in _MONTHLY_FAST_MASTER_FILTER_KEYS:
        if key.endswith("_list"):
            if _clean_list_param(params.get(key)):
                return "master_code_filter"
        elif like_value(params.get(key)) or clean_text(params.get(key)):
            return "master_name_filter"

    stock_codes = _clean_list_param(params.get("stock_cd_list"))
    if not stock_codes and clean_text(params.get("stock_cd")):
        stock_codes = [clean_text(params.get("stock_cd"))]
    if like_value(params.get("stock_nm")) and not stock_codes:
        return "stock_name_filter"

    return ""


def _can_use_monthly_fast_path(params: Dict[str, Any], source_mode: str) -> bool:
    return _monthly_fast_path_reason(params, source_mode) == ""


def _chunk_values(values: list[str], size: int = 1800):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _load_monthly_product_master_for_codes(product_codes: list[str]) -> pd.DataFrame:
    codes = sorted({clean_text(x) for x in product_codes if clean_text(x)})
    columns = [
        "제품코드",
        "제품명",
        "규격",
        "제조사코드",
        "제조사명",
        "제품그룹Gcode",
        "제품그룹코드",
        "제품그룹명",
        "제품구분Gcode",
        "제품구분코드",
        "제품구분명",
        "제품분류Gcode",
        "제품분류코드",
        "제품분류명",
    ]
    if not codes:
        return pd.DataFrame(columns=columns)

    frames: list[pd.DataFrame] = []
    for batch_idx, batch in enumerate(_chunk_values(codes)):
        bind_params: Dict[str, Any] = {}
        placeholders: list[str] = []
        for i, cd in enumerate(batch):
            key = f"physic_cd_{batch_idx}_{i}"
            bind_params[key] = cd
            placeholders.append(f"%({key})s")

        sql = f"""
SELECT
    P.Rd04_Physic_Cd AS [제품코드],
    P.Rd04_Physic_Nm AS [제품명],
    P.Rd04_Standard AS [규격],
    P.Rd04_Ven_Cd AS [제조사코드],
    Make_Ven.Rd03_Ven_Nm AS [제조사명],
    P.Rd04_Physic_Group_Gcode AS [제품그룹Gcode],
    P.Rd04_Physic_Group AS [제품그룹코드],
    Physic_Group_Nm.Rd01_Hnm AS [제품그룹명],
    P.Rd04_Physic_Di_Gcode AS [제품구분Gcode],
    P.Rd04_Physic_Di AS [제품구분코드],
    Physic_Di_Nm.Rd01_Hnm AS [제품구분명],
    P.Rd04_Physic_Tax_Gcode AS [제품분류Gcode],
    P.Rd04_Physic_Tax AS [제품분류코드],
    Physic_Tax_Nm.Rd01_Hnm AS [제품분류명]
FROM dbo.Rddbc040 AS P WITH (NOLOCK)
LEFT JOIN dbo.Rddbc030 AS Make_Ven WITH (NOLOCK)
    ON P.Rd04_Ven_Cd = Make_Ven.Rd03_Ven_Cd
LEFT JOIN dbo.Rddbc010 AS Physic_Group_Nm WITH (NOLOCK)
    ON Physic_Group_Nm.Rd01_Gcode = P.Rd04_Physic_Group_Gcode
   AND Physic_Group_Nm.Rd01_Tcode = P.Rd04_Physic_Group
LEFT JOIN dbo.Rddbc010 AS Physic_Di_Nm WITH (NOLOCK)
    ON Physic_Di_Nm.Rd01_Gcode = P.Rd04_Physic_Di_Gcode
   AND Physic_Di_Nm.Rd01_Tcode = P.Rd04_Physic_Di
LEFT JOIN dbo.Rddbc010 AS Physic_Tax_Nm WITH (NOLOCK)
    ON Physic_Tax_Nm.Rd01_Gcode = P.Rd04_Physic_Tax_Gcode
   AND Physic_Tax_Nm.Rd01_Tcode = P.Rd04_Physic_Tax
WHERE P.Rd04_Physic_Cd IN ({",".join(placeholders)})
"""
        df = query_to_df(sql, bind_params)
        if df is not None and not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame(columns=columns)

    out = pd.concat(frames, ignore_index=True)
    out["제품코드"] = out["제품코드"].fillna("").astype(str).str.strip()
    return out.drop_duplicates(subset=["제품코드"], keep="first")


def _load_monthly_vendor_names_for_codes(vendor_codes: list[str]) -> pd.DataFrame:
    codes = sorted({clean_text(x) for x in vendor_codes if clean_text(x)})
    columns = ["거래처코드", "거래처명"]
    if not codes:
        return pd.DataFrame(columns=columns)

    frames: list[pd.DataFrame] = []
    for batch_idx, batch in enumerate(_chunk_values(codes)):
        bind_params: Dict[str, Any] = {}
        placeholders: list[str] = []
        for i, cd in enumerate(batch):
            key = f"ven_cd_{batch_idx}_{i}"
            bind_params[key] = cd
            placeholders.append(f"%({key})s")

        sql = f"""
SELECT
    V.Rd03_Ven_Cd AS [거래처코드],
    V.Rd03_Ven_Nm AS [거래처명]
FROM dbo.Rddbc030 AS V WITH (NOLOCK)
WHERE V.Rd03_Ven_Cd IN ({",".join(placeholders)})
"""
        df = query_to_df(sql, bind_params)
        if df is not None and not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame(columns=columns)

    out = pd.concat(frames, ignore_index=True)
    out["거래처코드"] = out["거래처코드"].fillna("").astype(str).str.strip()
    return out.drop_duplicates(subset=["거래처코드"], keep="first")


def _build_monthly_fast_where(params: Dict[str, Any], spec: Dict[str, str]) -> tuple[str, Dict[str, Any]]:
    clauses: list[str] = []
    bind_params = dict(params)
    p = spec["prefix"]

    if clean_text(bind_params.get("month_from")):
        _add_filter(clauses, f"M.{p}_Stock_YyMm >= %(month_from)s")
    if clean_text(bind_params.get("month_to")):
        _add_filter(clauses, f"M.{p}_Stock_YyMm <= %(month_to)s")

    _add_filter(clauses, f"LEFT(M.{p}_Io_Gu, 1) IN ({spec['out_prefixes']})")

    if clean_text(bind_params.get("physic_cd")):
        _add_filter(clauses, f"M.{p}_Physic_Cd = %(physic_cd)s")

    stock_codes = _clean_list_param(bind_params.get("stock_cd_list"))
    if stock_codes:
        names: list[str] = []
        for i, cd in enumerate(stock_codes):
            key = f"fast_stock_cd_{i}"
            bind_params[key] = clean_text(cd)
            names.append(f"%({key})s")
        _add_filter(clauses, f"M.{p}_Stock_Cd IN ({','.join(names)})")
    elif clean_text(bind_params.get("stock_cd")):
        bind_params["stock_cd"] = clean_text(bind_params.get("stock_cd"))
        _add_filter(clauses, f"M.{p}_Stock_Cd = %(stock_cd)s")

    if clean_text(bind_params.get("buy_cd")):
        _add_filter(clauses, f"M.{p}_Ven_Cd = %(buy_cd)s")

    return ("\n  AND " + "\n  AND ".join(clauses)) if clauses else "", bind_params


def _get_sales_trend_monthly_df_fast(params: Optional[Dict[str, Any]] = None, source_mode: str = "monthly_book") -> pd.DataFrame:
    t0 = time.perf_counter()
    params = coalesce_params(params)
    params = _apply_month_or_date_params(params)

    spec = _monthly_spec(source_mode)
    p = spec["prefix"]
    where_sql, bind_params = _build_monthly_fast_where(params, spec)

    sql = f"""
SELECT
    LEFT(M.{p}_Stock_YyMm, 6) AS [기준월],
    M.{p}_Physic_Cd AS [제품코드],
    M.{p}_Ven_Cd AS [매입처코드],
    M.{p}_Stock_Apply_Cd AS [재고적용처코드],

    SUM(
        CASE
            WHEN LEFT(M.{p}_Io_Gu, 1) = '6'
            THEN -1 * COALESCE(M.{p}_Out_Quantity, 0)
            ELSE COALESCE(M.{p}_Out_Quantity, 0)
        END
    ) AS [출고수량],

    SUM(
        CASE
            WHEN LEFT(M.{p}_Io_Gu, 1) = '6'
            THEN -1 * COALESCE(M.{p}_Out_Oquantity, 0)
            ELSE COALESCE(M.{p}_Out_Oquantity, 0)
        END
    ) AS [출고할증수량],

    SUM(
        CASE
            WHEN LEFT(M.{p}_Io_Gu, 1) = '6'
            THEN -1 * COALESCE(M.{p}_Out_Supply_Price, 0)
            ELSE COALESCE(M.{p}_Out_Supply_Price, 0)
        END
    ) AS [매출공급가액],

    SUM(
        CASE
            WHEN LEFT(M.{p}_Io_Gu, 1) = '6'
            THEN -1 * COALESCE(M.{p}_Out_Tax_Price, 0)
            ELSE COALESCE(M.{p}_Out_Tax_Price, 0)
        END
    ) AS [매출세액],

    SUM(
        CASE
            WHEN LEFT(M.{p}_Io_Gu, 1) = '6'
            THEN -1 * (
                COALESCE(M.{p}_Out_Supply_Price, 0)
                + COALESCE(M.{p}_Out_Tax_Price, 0)
            )
            ELSE (
                COALESCE(M.{p}_Out_Supply_Price, 0)
                + COALESCE(M.{p}_Out_Tax_Price, 0)
            )
        END
    ) AS [매출합계],

    COUNT(*) AS [집계건수],
    COUNT(DISTINCT M.{p}_Ven_Cd) AS [매입처수],
    '{spec["title"]}' AS [분석자료원]

FROM {spec["table"]} AS M WITH (NOLOCK)

WHERE 1 = 1
{where_sql}

GROUP BY
    LEFT(M.{p}_Stock_YyMm, 6),
    M.{p}_Physic_Cd,
    M.{p}_Ven_Cd,
    M.{p}_Stock_Apply_Cd

ORDER BY
    M.{p}_Physic_Cd,
    LEFT(M.{p}_Stock_YyMm, 6),
    M.{p}_Ven_Cd
OPTION (RECOMPILE)
"""

    raw_df = query_to_df(sql, bind_params)
    t_monthly = time.perf_counter()
    if raw_df is None:
        raw_df = pd.DataFrame()

    product_code_count = int(raw_df["제품코드"].nunique()) if "제품코드" in raw_df.columns else 0
    vendor_codes: set[str] = set()
    if "매입처코드" in raw_df.columns:
        vendor_codes.update(raw_df["매입처코드"].fillna("").astype(str).str.strip().tolist())
    if "재고적용처코드" in raw_df.columns:
        vendor_codes.update(raw_df["재고적용처코드"].fillna("").astype(str).str.strip().tolist())
    vendor_codes.discard("")

    product_df = _load_monthly_product_master_for_codes(
        raw_df["제품코드"].fillna("").astype(str).str.strip().tolist()
        if "제품코드" in raw_df.columns
        else []
    )
    t_product = time.perf_counter()

    vendor_df = _load_monthly_vendor_names_for_codes(list(vendor_codes))
    t_vendor = time.perf_counter()

    merged = raw_df.copy()
    if product_df is not None and not product_df.empty:
        merged["제품코드"] = merged["제품코드"].fillna("").astype(str).str.strip()
        merged = merged.merge(product_df, on="제품코드", how="left")
    if vendor_df is not None and not vendor_df.empty:
        for col in ["매입처코드", "재고적용처코드"]:
            if col in merged.columns:
                merged[col] = merged[col].fillna("").astype(str).str.strip()
        merged = merged.merge(
            vendor_df.rename(columns={"거래처코드": "매입처코드", "거래처명": "매입처명"}),
            on="매입처코드",
            how="left",
        )
        merged = merged.merge(
            vendor_df.rename(columns={"거래처코드": "재고적용처코드", "거래처명": "재고적용처명"}),
            on="재고적용처코드",
            how="left",
        )

    final_cols = [
        "기준월",
        "제품코드",
        "제품명",
        "규격",
        "제조사코드",
        "제조사명",
        "제품그룹Gcode",
        "제품그룹코드",
        "제품그룹명",
        "제품구분Gcode",
        "제품구분코드",
        "제품구분명",
        "제품분류Gcode",
        "제품분류코드",
        "제품분류명",
        "매입처코드",
        "매입처명",
        "재고적용처코드",
        "재고적용처명",
        "출고수량",
        "출고할증수량",
        "매출공급가액",
        "매출세액",
        "매출합계",
        "집계건수",
        "매입처수",
        "분석자료원",
    ]
    for col in final_cols:
        if col not in merged.columns:
            merged[col] = ""
    merged = merged[final_cols]
    t_merge = time.perf_counter()

    df = _add_trend_columns(merged)
    df = _normalize_analytics_numeric_columns(df)
    t_done = time.perf_counter()
    log.info(
        "[analytics.sales_trend.fast_path] enabled=True source_mode=%s raw_rows=%s product_codes=%s vendor_codes=%s monthly_sql=%.3fs product_master=%.3fs vendor_master=%.3fs merge=%.3fs total=%.3fs",
        source_mode,
        0 if raw_df is None else len(raw_df),
        product_code_count,
        len(vendor_codes),
        t_monthly - t0,
        t_product - t_monthly,
        t_vendor - t_product,
        t_merge - t_vendor,
        t_done - t0,
    )
    return df


def get_sales_trend_monthly_df(params: Optional[Dict[str, Any]] = None, source_mode: str = "monthly_book") -> pd.DataFrame:
    params = coalesce_params(params)
    params = _apply_month_or_date_params(params)
    reason = _monthly_fast_path_reason(params, source_mode)
    if not reason:
        return _get_sales_trend_monthly_df_fast(params, source_mode=source_mode)

    log.info(
        "[analytics.sales_trend.fast_path] enabled=False reason=%s source_mode=%s",
        reason,
        source_mode,
    )
    return _get_sales_trend_monthly_df_legacy(params, source_mode=source_mode)


def _get_sales_trend_monthly_df_legacy(params: Optional[Dict[str, Any]] = None, source_mode: str = "monthly_book") -> pd.DataFrame:
    params = coalesce_params(params)
    params = _apply_month_or_date_params(params)

    spec = _monthly_spec(source_mode)
    p = spec["prefix"]
    a = spec["alias"]
    where_sql = _build_monthly_filters(params, spec)

    sql = f"""
SELECT
    LEFT({a}.{p}_Stock_YyMm, 6) AS 기준월,

    {a}.{p}_Physic_Cd AS 제품코드,
    Physic_Cd.Rd04_Physic_Nm AS 제품명,
    Physic_Cd.Rd04_Standard AS 규격,

    Physic_Cd.Rd04_Ven_Cd AS 제조사코드,
    Make_Ven.Rd03_Ven_Nm AS 제조사명,

    Physic_Cd.Rd04_Physic_Group_Gcode AS 제품그룹Gcode,
    Physic_Cd.Rd04_Physic_Group AS 제품그룹코드,
    Physic_Group_Nm.Rd01_Hnm AS 제품그룹명,
    Physic_Cd.Rd04_Physic_Di_Gcode AS 제품구분Gcode,
    Physic_Cd.Rd04_Physic_Di AS 제품구분코드,
    Physic_Di_Nm.Rd01_Hnm AS 제품구분명,
    Physic_Cd.Rd04_Physic_Tax_Gcode AS 제품분류Gcode,
    Physic_Cd.Rd04_Physic_Tax AS 제품분류코드,
    Physic_Tax_Nm.Rd01_Hnm AS 제품분류명,

    {a}.{p}_Ven_Cd AS 매입처코드,
    Buy_Ven.Rd03_Ven_Nm AS 매입처명,

    {a}.{p}_Stock_Apply_Cd AS 재고적용처코드,
    Stock_Apply.Rd03_Ven_Nm AS 재고적용처명,

    SUM(
        CASE
            WHEN LEFT({a}.{p}_Io_Gu, 1) = '6'
            THEN -1 * COALESCE({a}.{p}_Out_Quantity, 0)
            ELSE COALESCE({a}.{p}_Out_Quantity, 0)
        END
    ) AS 출고수량,

    SUM(
        CASE
            WHEN LEFT({a}.{p}_Io_Gu, 1) = '6'
            THEN -1 * COALESCE({a}.{p}_Out_Oquantity, 0)
            ELSE COALESCE({a}.{p}_Out_Oquantity, 0)
        END
    ) AS 출고할증수량,

    SUM(
        CASE
            WHEN LEFT({a}.{p}_Io_Gu, 1) = '6'
            THEN -1 * COALESCE({a}.{p}_Out_Supply_Price, 0)
            ELSE COALESCE({a}.{p}_Out_Supply_Price, 0)
        END
    ) AS 매출공급가액,

    SUM(
        CASE
            WHEN LEFT({a}.{p}_Io_Gu, 1) = '6'
            THEN -1 * COALESCE({a}.{p}_Out_Tax_Price, 0)
            ELSE COALESCE({a}.{p}_Out_Tax_Price, 0)
        END
    ) AS 매출세액,

    SUM(
        CASE
            WHEN LEFT({a}.{p}_Io_Gu, 1) = '6'
            THEN -1 * (
                COALESCE({a}.{p}_Out_Supply_Price, 0)
                + COALESCE({a}.{p}_Out_Tax_Price, 0)
            )
            ELSE (
                COALESCE({a}.{p}_Out_Supply_Price, 0)
                + COALESCE({a}.{p}_Out_Tax_Price, 0)
            )
        END
    ) AS 매출합계,

    COUNT(*) AS 집계건수,
    COUNT(DISTINCT {a}.{p}_Ven_Cd) AS 매입처수,

    '{spec["title"]}' AS 분석자료원

FROM {spec["table"]} AS {a} WITH (NOLOCK)

LEFT JOIN dbo.Rddbc040 AS Physic_Cd WITH (NOLOCK)
    ON {a}.{p}_Physic_Cd = Physic_Cd.Rd04_Physic_Cd

LEFT JOIN dbo.Rddbc030 AS Make_Ven WITH (NOLOCK)
    ON Physic_Cd.Rd04_Ven_Cd = Make_Ven.Rd03_Ven_Cd

LEFT JOIN dbo.Rddbc030 AS Buy_Ven WITH (NOLOCK)
    ON {a}.{p}_Ven_Cd = Buy_Ven.Rd03_Ven_Cd

LEFT JOIN dbo.Rddbc030 AS Stock_Apply WITH (NOLOCK)
    ON {a}.{p}_Stock_Apply_Cd = Stock_Apply.Rd03_Ven_Cd

LEFT JOIN dbo.Rddbc010 AS Physic_Group_Nm WITH (NOLOCK)
    ON Physic_Group_Nm.Rd01_Gcode = Physic_Cd.Rd04_Physic_Group_Gcode
   AND Physic_Group_Nm.Rd01_Tcode = Physic_Cd.Rd04_Physic_Group

LEFT JOIN dbo.Rddbc010 AS Physic_Di_Nm WITH (NOLOCK)
    ON Physic_Di_Nm.Rd01_Gcode = Physic_Cd.Rd04_Physic_Di_Gcode
   AND Physic_Di_Nm.Rd01_Tcode = Physic_Cd.Rd04_Physic_Di

LEFT JOIN dbo.Rddbc010 AS Physic_Tax_Nm WITH (NOLOCK)
    ON Physic_Tax_Nm.Rd01_Gcode = Physic_Cd.Rd04_Physic_Tax_Gcode
   AND Physic_Tax_Nm.Rd01_Tcode = Physic_Cd.Rd04_Physic_Tax

LEFT JOIN dbo.Rddbc010 AS Stock_Cd WITH (NOLOCK)
    ON {a}.{p}_Stock_Cd_Gcode = Stock_Cd.Rd01_Gcode
   AND {a}.{p}_Stock_Cd = Stock_Cd.Rd01_Tcode

WHERE 1 = 1
{where_sql}

GROUP BY
    LEFT({a}.{p}_Stock_YyMm, 6),

    {a}.{p}_Physic_Cd,
    Physic_Cd.Rd04_Physic_Nm,
    Physic_Cd.Rd04_Standard,

    Physic_Cd.Rd04_Ven_Cd,
    Make_Ven.Rd03_Ven_Nm,

    Physic_Cd.Rd04_Physic_Group_Gcode,
    Physic_Cd.Rd04_Physic_Group,
    Physic_Group_Nm.Rd01_Hnm,
    Physic_Cd.Rd04_Physic_Di_Gcode,
    Physic_Cd.Rd04_Physic_Di,
    Physic_Di_Nm.Rd01_Hnm,
    Physic_Cd.Rd04_Physic_Tax_Gcode,
    Physic_Cd.Rd04_Physic_Tax,
    Physic_Tax_Nm.Rd01_Hnm,

    {a}.{p}_Ven_Cd,
    Buy_Ven.Rd03_Ven_Nm,

    {a}.{p}_Stock_Apply_Cd,
    Stock_Apply.Rd03_Ven_Nm

ORDER BY
    제품코드,
    기준월,
    매입처코드
"""

    df = query_to_df(sql, params)
    if df is None:
        df = pd.DataFrame()

    df = _add_trend_columns(df)
    df = _normalize_analytics_numeric_columns(df)
    return df


def _build_sales_month_workforward_metrics(df: pd.DataFrame, params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    if df is None or df.empty or "제품코드" not in df.columns or "기준월" not in df.columns:
        return pd.DataFrame()

    work = df.copy()
    work["기준월"] = work["기준월"].map(_parse_yyyymm)
    for col in ["출고수량", "매출합계"]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)
        else:
            work[col] = 0

    monthly = (
        work.groupby(["제품코드", "기준월"], dropna=False, as_index=False)[["출고수량", "매출합계"]]
        .sum()
        .sort_values(["제품코드", "기준월"])
        .reset_index(drop=True)
    )

    months = sorted({_parse_yyyymm(v) for v in monthly["기준월"].dropna().tolist()})
    completed_months, current_month, future_months = _split_sales_period_months(months, params)
    completed_set = set(completed_months)
    future_set = set(future_months)

    def _period_label(value: Any) -> str:
        m = _parse_yyyymm(value)
        if m in completed_set:
            return "완료월"
        if current_month and m == current_month:
            return "당월진행"
        if m in future_set:
            return "미래월"
        return "완료월"

    monthly_grp = monthly.groupby("제품코드", dropna=False)
    monthly["기간구분"] = monthly["기준월"].map(_period_label)
    monthly["전월대비수량"] = monthly_grp["출고수량"].diff().fillna(0)
    monthly["전월대비매출"] = monthly_grp["매출합계"].diff().fillna(0)
    monthly["_이전월매출"] = monthly_grp["매출합계"].shift(1)
    monthly["월시점 완료월수"] = monthly_grp.cumcount()
    monthly["월시점 완료월총매출"] = monthly_grp["매출합계"].cumsum().shift(1)
    monthly["월시점 완료월총매출"] = monthly["월시점 완료월총매출"].where(monthly["월시점 완료월수"] > 0, 0).fillna(0)
    monthly["월시점 완료월평균매출"] = (
        monthly["월시점 완료월총매출"] / monthly["월시점 완료월수"].replace(0, 1)
    ).where(monthly["월시점 완료월수"] > 0, 0)
    prev_nonzero_sales = monthly["_이전월매출"].fillna(0).ne(0).astype(int)
    monthly["월시점 매출발생월수"] = (
        prev_nonzero_sales.groupby(monthly["제품코드"], dropna=False).cumsum()
    )
    monthly["월시점 최근3개월평균매출"] = (
        monthly.groupby("제품코드", dropna=False)["_이전월매출"]
        .rolling(3, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
        .fillna(0)
    )
    monthly["월시점 최근6개월평균매출"] = (
        monthly.groupby("제품코드", dropna=False)["_이전월매출"]
        .rolling(6, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
        .fillna(0)
    )
    monthly["최근3개월평균매출"] = monthly["월시점 최근3개월평균매출"]
    monthly["최근6개월평균매출"] = monthly["월시점 최근6개월평균매출"]
    monthly["월시점 증감률"] = [
        _pct_change(r3, r6)
        for r3, r6 in zip(monthly["월시점 최근3개월평균매출"].tolist(), monthly["월시점 최근6개월평균매출"].tolist())
    ]
    prev_negative_seen = (
        monthly.assign(_neg=monthly["매출합계"].lt(0))
        .groupby("제품코드", dropna=False)["_neg"]
        .cummax()
        .shift(1)
    )
    monthly["_누계반품월여부"] = prev_negative_seen.eq(True) & monthly["월시점 완료월수"].gt(0)
    monthly["월시점 추세판정"] = monthly.apply(lambda r: _month_point_trend_judge(r), axis=1)
    monthly["월시점 판정결과"] = monthly["월시점 추세판정"]
    monthly["추세판정"] = monthly["월시점 추세판정"]
    monthly["판정결과"] = monthly["월시점 판정결과"]
    monthly["월시점 실제매출"] = monthly["매출합계"]

    projection_src = pd.DataFrame({
        "완료월총매출": monthly["월시점 완료월총매출"],
        "완료월수": monthly["월시점 완료월수"],
        "완료월평균매출": monthly["월시점 완료월평균매출"],
        "월평균매출": monthly["월시점 완료월평균매출"],
        "최근3개월평균매출": monthly["월시점 최근3개월평균매출"],
        "최근6개월평균매출": monthly["월시점 최근6개월평균매출"],
        "최근3개월증감률": monthly["월시점 증감률"],
        "매출발생월수": monthly["월시점 매출발생월수"],
        "추세판정": monthly["월시점 추세판정"],
    })
    projection = projection_src.apply(lambda r: _forecast_projection_from_row(r), axis=1, result_type="expand")
    if not projection.empty:
        projection.columns = ["월시점 예상기준", "월시점 적용증감률", "월시점 예상매출"]
        monthly["월시점 예상기준"] = projection["월시점 예상기준"]
        monthly["월시점 적용증감률"] = projection["월시점 적용증감률"]
        monthly["월시점 예상매출"] = projection["월시점 예상매출"]
    else:
        monthly["월시점 예상기준"] = "자료부족"
        monthly["월시점 적용증감률"] = 0
        monthly["월시점 예상매출"] = 0
    monthly["월시점 예상대비차이"] = monthly["월시점 실제매출"] - monthly["월시점 예상매출"]
    monthly["월시점 잔여예상"] = (monthly["월시점 예상매출"] - monthly["월시점 실제매출"]).clip(lower=0)
    monthly["월시점 달성률"] = [
        (actual / expected * 100) if abs(float(expected or 0)) >= 1e-12 else 0
        for actual, expected in zip(monthly["월시점 실제매출"].tolist(), monthly["월시점 예상매출"].tolist())
    ]
    return monthly.drop(columns=[c for c in ["_이전월매출", "_누계반품월여부"] if c in monthly.columns])


def _add_trend_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    out = df.copy()

    num_cols = [
        "출고수량",
        "출고할증수량",
        "매출공급가액",
        "매출세액",
        "매출합계",
    ]

    for col in num_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)

    if "제품코드" not in out.columns or "기준월" not in out.columns:
        return out

    out["기준월"] = out["기준월"].map(_parse_yyyymm)
    out = out.sort_values(["제품코드", "기준월"]).reset_index(drop=True)
    monthly = _build_sales_month_workforward_metrics(out)
    derived_cols = [
        "기간구분",
        "전월대비수량",
        "전월대비매출",
        "최근3개월평균매출",
        "최근6개월평균매출",
        "월시점 완료월수",
        "월시점 완료월평균매출",
        "월시점 최근3개월평균매출",
        "월시점 최근6개월평균매출",
        "월시점 증감률",
        "월시점 추세판정",
        "월시점 판정결과",
        "월시점 실제매출",
        "월시점 예상기준",
        "월시점 적용증감률",
        "월시점 예상매출",
        "월시점 예상대비차이",
        "월시점 잔여예상",
        "월시점 달성률",
        "추세판정",
        "판정결과",
    ]
    out = out.drop(columns=[c for c in derived_cols if c in out.columns])
    if not monthly.empty:
        monthly_for_merge = monthly[["제품코드", "기준월"] + [c for c in derived_cols if c in monthly.columns]].copy()
        out = out.merge(
            monthly_for_merge,
            on=["제품코드", "기준월"],
            how="left",
            validate="many_to_one",
        )

    if "매출공급가액" in out.columns and "출고수량" in out.columns:
        out["평균공급단가"] = out.apply(
            lambda r: (float(r["매출공급가액"]) / float(r["출고수량"]))
            if float(r["출고수량"] or 0) != 0
            else 0,
            axis=1,
        )

    return out


def _month_point_trend_judge(row: pd.Series) -> str:
    completed_count = int(float(row.get("월시점 완료월수") or 0))
    if completed_count <= 0:
        return "자료부족"

    return _trend_judge(
        float(row.get("월시점 완료월총매출") or 0),
        float(row.get("월시점 최근3개월평균매출") or 0),
        float(row.get("월시점 최근6개월평균매출") or 0),
        bool(row.get("_누계반품월여부")),
    )


def get_sales_trend_detail_df(params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    params = coalesce_params(params)
    params = _apply_month_or_date_params(params)

    where_sql = _build_filters(params)

    sql = f"""
SELECT
    LEFT(Out_Put.Rd12_Out_YyMmDd, 6) AS 기준월,

    Out_Put.Rd12_Physic_Cd AS 제품코드,
    Physic_Cd.Rd04_Physic_Nm AS 제품명,
    Physic_Cd.Rd04_Standard AS 규격,

    Physic_Cd.Rd04_Ven_Cd AS 제조사코드,
    Make_Ven.Rd03_Ven_Nm AS 제조사명,

    Physic_Cd.Rd04_Physic_Group_Gcode AS 제품그룹Gcode,
    Physic_Cd.Rd04_Physic_Group AS 제품그룹코드,
    Physic_Group_Nm.Rd01_Hnm AS 제품그룹명,
    Physic_Cd.Rd04_Physic_Di_Gcode AS 제품구분Gcode,
    Physic_Cd.Rd04_Physic_Di AS 제품구분코드,
    Physic_Di_Nm.Rd01_Hnm AS 제품구분명,
    Physic_Cd.Rd04_Physic_Tax_Gcode AS 제품분류Gcode,
    Physic_Cd.Rd04_Physic_Tax AS 제품분류코드,
    Physic_Tax_Nm.Rd01_Hnm AS 제품분류명,

    Out_Put.Rd12_Ven_Cd AS 거래처코드,
    Ven_Cd.Rd03_Ven_Nm AS 거래처명,

    Out_Put.Rd12_In_Ven_Cd AS 매입처코드,
    In_Ven_Cd.Rd03_Ven_Nm AS 매입처명,

    Road1.Rd021_Sido AS 시도명,
    Road1.Rd021_Gugun AS 시구군명,
    Road1.Rd021_DongNm AS 법정읍면동명,

    SUM(
        CASE
            WHEN LEFT(Out_Put.Rd12_Io_Gu, 1) = '6'
            THEN -1 * COALESCE(Out_Put.Rd12_Quantity, 0)
            ELSE COALESCE(Out_Put.Rd12_Quantity, 0)
        END
    ) AS 출고수량,

    SUM(
        CASE
            WHEN LEFT(Out_Put.Rd12_Io_Gu, 1) = '6'
            THEN -1 * COALESCE(Out_Put.Rd12_Oquantity, 0)
            ELSE COALESCE(Out_Put.Rd12_Oquantity, 0)
        END
    ) AS 출고할증수량,

    SUM(
        CASE
            WHEN LEFT(Out_Put.Rd12_Io_Gu, 1) = '6'
            THEN -1 * COALESCE(Out_Put.Rd12_Fin_Supply_Price, Out_Put.Rd12_Supply_Price, 0)
            ELSE COALESCE(Out_Put.Rd12_Fin_Supply_Price, Out_Put.Rd12_Supply_Price, 0)
        END
    ) AS 매출공급가액,

    SUM(
        CASE
            WHEN LEFT(Out_Put.Rd12_Io_Gu, 1) = '6'
            THEN -1 * COALESCE(Out_Put.Rd12_Fin_Tax_Price, Out_Put.Rd12_Tax_Price, 0)
            ELSE COALESCE(Out_Put.Rd12_Fin_Tax_Price, Out_Put.Rd12_Tax_Price, 0)
        END
    ) AS 매출세액,

    SUM(
        CASE
            WHEN LEFT(Out_Put.Rd12_Io_Gu, 1) = '6'
            THEN -1 * (
                COALESCE(Out_Put.Rd12_Fin_Supply_Price, Out_Put.Rd12_Supply_Price, 0)
                + COALESCE(Out_Put.Rd12_Fin_Tax_Price, Out_Put.Rd12_Tax_Price, 0)
            )
            ELSE (
                COALESCE(Out_Put.Rd12_Fin_Supply_Price, Out_Put.Rd12_Supply_Price, 0)
                + COALESCE(Out_Put.Rd12_Fin_Tax_Price, Out_Put.Rd12_Tax_Price, 0)
            )
        END
    ) AS 매출합계,

    COUNT(*) AS 출고건수,
    COUNT(DISTINCT Out_Put.Rd12_Ven_Cd) AS 거래처수

FROM dbo.Rddbc120 AS Out_Put WITH (NOLOCK)

LEFT JOIN dbo.Rddbc030 AS Ven_Cd WITH (NOLOCK)
    ON Out_Put.Rd12_Ven_Cd = Ven_Cd.Rd03_Ven_Cd

LEFT JOIN dbo.Rddbc030 AS In_Ven_Cd WITH (NOLOCK)
    ON Out_Put.Rd12_In_Ven_Cd = In_Ven_Cd.Rd03_Ven_Cd

LEFT JOIN dbo.Rddbc030 AS Real_Ven_Cd WITH (NOLOCK)
    ON Out_Put.Rd12_Real_Ven_Cd = Real_Ven_Cd.Rd03_Ven_Cd

LEFT JOIN dbo.Rddbc040 AS Physic_Cd WITH (NOLOCK)
    ON Out_Put.Rd12_Physic_Cd = Physic_Cd.Rd04_Physic_Cd

LEFT JOIN dbo.Rddbc030 AS Make_Ven WITH (NOLOCK)
    ON Physic_Cd.Rd04_Ven_Cd = Make_Ven.Rd03_Ven_Cd

LEFT JOIN dbo.Rddbc010 AS Physic_Group_Nm WITH (NOLOCK)
    ON Physic_Group_Nm.Rd01_Gcode = Physic_Cd.Rd04_Physic_Group_Gcode
   AND Physic_Group_Nm.Rd01_Tcode = Physic_Cd.Rd04_Physic_Group

LEFT JOIN dbo.Rddbc010 AS Physic_Di_Nm WITH (NOLOCK)
    ON Physic_Di_Nm.Rd01_Gcode = Physic_Cd.Rd04_Physic_Di_Gcode
   AND Physic_Di_Nm.Rd01_Tcode = Physic_Cd.Rd04_Physic_Di

LEFT JOIN dbo.Rddbc010 AS Physic_Tax_Nm WITH (NOLOCK)
    ON Physic_Tax_Nm.Rd01_Gcode = Physic_Cd.Rd04_Physic_Tax_Gcode
   AND Physic_Tax_Nm.Rd01_Tcode = Physic_Cd.Rd04_Physic_Tax

LEFT JOIN dbo.Rddbc010 AS Stock_Cd WITH (NOLOCK)
    ON Out_Put.Rd12_Stock_Cd_Gcode = Stock_Cd.Rd01_Gcode
   AND Out_Put.Rd12_Stock_Cd = Stock_Cd.Rd01_Tcode

LEFT JOIN dbo.Rddbc060 AS Sales_Man WITH (NOLOCK)
    ON Out_Put.Rd12_Sales_Man = Sales_Man.Rd06_User_Cd

LEFT JOIN dbo.Rddbc021 AS Road1 WITH (NOLOCK)
    ON LTRIM(RTRIM(Road1.Rd021_RoadCd)) = LTRIM(RTRIM(Ven_Cd.Rd03_RoadCd))
   AND LTRIM(RTRIM(Road1.Rd021_DongSeq)) = LTRIM(RTRIM(Ven_Cd.Rd03_DongSeq))

WHERE 1 = 1
{where_sql}

GROUP BY
    LEFT(Out_Put.Rd12_Out_YyMmDd, 6),

    Out_Put.Rd12_Physic_Cd,
    Physic_Cd.Rd04_Physic_Nm,
    Physic_Cd.Rd04_Standard,

    Physic_Cd.Rd04_Ven_Cd,
    Make_Ven.Rd03_Ven_Nm,

    Physic_Cd.Rd04_Physic_Group_Gcode,
    Physic_Cd.Rd04_Physic_Group,
    Physic_Group_Nm.Rd01_Hnm,
    Physic_Cd.Rd04_Physic_Di_Gcode,
    Physic_Cd.Rd04_Physic_Di,
    Physic_Di_Nm.Rd01_Hnm,
    Physic_Cd.Rd04_Physic_Tax_Gcode,
    Physic_Cd.Rd04_Physic_Tax,
    Physic_Tax_Nm.Rd01_Hnm,

    Out_Put.Rd12_Ven_Cd,
    Ven_Cd.Rd03_Ven_Nm,

    Out_Put.Rd12_In_Ven_Cd,
    In_Ven_Cd.Rd03_Ven_Nm,

    Road1.Rd021_Sido,
    Road1.Rd021_Gugun,
    Road1.Rd021_DongNm

ORDER BY
    제품코드,
    기준월,
    거래처코드
"""

    df = query_to_df(sql, params)

    if df is None:
        df = pd.DataFrame()

    df = _add_trend_columns(df)
    df = _normalize_analytics_numeric_columns(df)
    return df


def _apply_monthly_current_detail_mix(
    monthly_df: pd.DataFrame,
    params: Dict[str, Any],
    *,
    source_mode: str,
    source_policy: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """
    월중 종료일 조회에서 완료월은 월집계, 종료월은 출고상세로 교체한다.

    월집계의 종료월 행과 상세 종료월 행을 더하면 중복 집계되므로,
    종료월 월집계 행은 제거하고 Rddbc120의 1일~date_to 집계만 붙인다.
    """
    if monthly_df is None or monthly_df.empty:
        return monthly_df

    policy = source_policy or params.get("_period_source_policy") or _resolve_period_source_policy(params)
    if not policy.get("use_hybrid"):
        return monthly_df

    date_to = str(policy.get("effective_date_to") or "")
    if not date_to:
        return monthly_df

    end_month = date_to[:6]
    detail_params = dict(params)
    detail_params["source_mode"] = "detail"
    detail_params["date_from"] = f"{end_month}01"
    detail_params["date_to"] = date_to
    detail_params["month_from"] = end_month
    detail_params["month_to"] = end_month
    detail_df = get_sales_trend_detail_df(detail_params)

    work = monthly_df.copy()
    if "기준월" in work.columns:
        work["_기준월_정규화"] = work["기준월"].map(_parse_yyyymm)
        work = work[work["_기준월_정규화"] != end_month].drop(columns=["_기준월_정규화"])

    if detail_df is not None and not detail_df.empty:
        detail_df = detail_df.copy()
        detail_df["분석자료원"] = "출고상세(Rddbc120)"
        for col in work.columns:
            if col not in detail_df.columns:
                detail_df[col] = ""
        for col in detail_df.columns:
            if col not in work.columns:
                work[col] = ""
        mixed = pd.concat([work, detail_df[work.columns]], ignore_index=True, sort=False)
    else:
        mixed = work

    if mixed.empty:
        return mixed

    mixed = _add_trend_columns(mixed)
    mixed = _normalize_analytics_numeric_columns(mixed)
    mixed.attrs.update(monthly_df.attrs)
    mixed.attrs["mixed_current_month_detail"] = True
    mixed.attrs["mixed_current_month"] = end_month
    mixed.attrs["source_label_completed"] = _source_label(source_mode)
    mixed.attrs["source_label_current"] = "출고상세(Rddbc120)"
    return mixed

def get_sales_trend_df(params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    t0 = time.perf_counter()
    params = coalesce_params(params)
    params = _apply_month_or_date_params(params)
    params = _apply_period_source_policy_params(params)
    source_policy = params.get("_period_source_policy") or {}

    source_mode = _resolve_source_mode(params)
    params["resolved_source_mode"] = source_mode

    if source_mode in {"monthly_book", "monthly_real"}:
        df = get_sales_trend_monthly_df(params, source_mode=source_mode)
        df = _apply_monthly_current_detail_mix(df, params, source_mode=source_mode, source_policy=source_policy)
    else:
        df = get_sales_trend_detail_df(params)

    log.info(
        "[analytics.sales_trend.perf] source_mode=%s rows=%s elapsed=%.3fs",
        source_mode,
        0 if df is None else len(df),
        time.perf_counter() - t0,
    )
    return _finalize_sales_trend_public_df(df)

def get_sales_trend_summary_df(
    params: Optional[Dict[str, Any]] = None,
    raw_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    품목별 매출 추세 요약표.
    기존 get_sales_trend_df() 결과를 제품 1줄 단위로 재집계한다.

    원자료 단위:
      - 월집계: 기준월 + 제품 + 매입처
      - 출고상세: 기준월 + 제품 + 거래처

    요약표 단위:
      - 제품코드 + 제품명 + 규격 + 제조사명 + 제품그룹/구분/분류
    """
    t0 = time.perf_counter()
    params = coalesce_params(params)
    params = _apply_month_or_date_params(params)
    params = _apply_period_source_policy_params(params)

    raw = raw_df if raw_df is not None else get_sales_trend_df(params)
    t_raw = time.perf_counter()
    if raw is None or raw.empty:
        log.info(
            "[analytics.sales_trend_summary.perf] raw_rows=%s out_rows=0 months=0 raw=%.3fs group=0.000s pivot_sales=0.000s pivot_qty=0.000s merge=0.000s calc=0.000s sort=0.000s total=%.3fs",
            0 if raw is None else len(raw),
            t_raw - t0,
            time.perf_counter() - t0,
        )
        return pd.DataFrame()

    df = raw.copy()

    if "기준월" not in df.columns or "제품코드" not in df.columns:
        return pd.DataFrame()

    df["기준월"] = df["기준월"].map(_parse_yyyymm)

    for c in ["출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    product_cols = [
        "제품코드",
        "제품명",
        "규격",
        "제조사코드",
        "제조사명",
        "제품그룹명",
        "제품구분명",
        "제품분류명",
    ]
    product_cols = [c for c in product_cols if c in df.columns]

    months = _month_list_for_summary(params, df)

    # 제품별 기본 합계
    agg_dict: Dict[str, Any] = {}

    for c in ["출고수량", "출고할증수량", "매출공급가액", "매출세액", "매출합계"]:
        if c in df.columns:
            agg_dict[c] = "sum"

    if "출고건수" in df.columns:
        agg_dict["출고건수"] = "sum"
    if "집계건수" in df.columns:
        agg_dict["집계건수"] = "sum"

    # 월집계 + 당월 상세 혼합 시 숫자 컬럼의 str/int 혼합 방지
    # agg_dict에서 합산하는 모든 컬럼을 groupby 전에 숫자형으로 통일한다.
    for col, agg_func in agg_dict.items():
        if agg_func != "sum" or col not in df.columns:
            continue

        raw_series = df[col]

        if not pd.api.types.is_numeric_dtype(raw_series):
            raw_series = (
                raw_series.astype(str)
                .str.replace(",", "", regex=False)
                .str.strip()
            )

        df[col] = pd.to_numeric(
            raw_series,
            errors="coerce",
        ).fillna(0)

    base = (
        df.groupby(product_cols, dropna=False)
        .agg(agg_dict)
        .reset_index()
    )

    base = base.rename(columns={
        "출고수량": "총출고수량",
        "출고할증수량": "총출고할증수량",
        "매출공급가액": "총매출공급가액",
        "매출세액": "총매출세액",
        "매출합계": "총매출액",
        "출고건수": "총출고건수",
        "집계건수": "총집계건수",
    })

    # 거래처/매입처 수
    count_col = None
    count_label = None
    if "거래처코드" in df.columns:
        count_col = "거래처코드"
        count_label = "거래처수"
    elif "매입처코드" in df.columns:
        count_col = "매입처코드"
        count_label = "매입처수"
    elif "재고적용처코드" in df.columns:
        count_col = "재고적용처코드"
        count_label = "재고적용처수"

    if count_col:
        cnt = (
            df.groupby(product_cols, dropna=False)[count_col]
            .nunique()
            .reset_index()
            .rename(columns={count_col: count_label})
        )
        base = base.merge(cnt, on=product_cols, how="left")
    t_group = time.perf_counter()

    # 월별 매출/수량 pivot
    pivot_sales = (
        df.pivot_table(
            index=product_cols,
            columns="기준월",
            values="매출합계",
            aggfunc="sum",
            fill_value=0,
        )
        .reset_index()
    )

    t_pivot_sales = time.perf_counter()

    pivot_qty = (
        df.pivot_table(
            index=product_cols,
            columns="기준월",
            values="출고수량",
            aggfunc="sum",
            fill_value=0,
        )
        .reset_index()
    )

    t_pivot_qty = time.perf_counter()

    # 누락월 0 채우기
    for m in months:
        if m not in pivot_sales.columns:
            pivot_sales[m] = 0
        if m not in pivot_qty.columns:
            pivot_qty[m] = 0

    if months:
        pivot_sales = pivot_sales[product_cols + months]
        pivot_qty = pivot_qty[product_cols + months]

    sales_rename = {m: f"{_fmt_yyyymm_col(m)} 매출" for m in months}
    qty_rename = {m: f"{_fmt_yyyymm_col(m)} 수량" for m in months}

    pivot_sales = pivot_sales.rename(columns=sales_rename)
    pivot_qty = pivot_qty.rename(columns=qty_rename)

    out = base.merge(pivot_sales, on=product_cols, how="left")
    out = out.merge(pivot_qty, on=product_cols, how="left")
    t_merge = time.perf_counter()

    # 월별 컬럼 NaN 보정
    month_sales_cols = [f"{_fmt_yyyymm_col(m)} 매출" for m in months]
    month_qty_cols = [f"{_fmt_yyyymm_col(m)} 수량" for m in months]
    completed_months, current_month, future_months = _split_sales_period_months(months, params)
    completed_sales_cols = [
        f"{_fmt_yyyymm_col(m)} 매출"
        for m in completed_months
        if f"{_fmt_yyyymm_col(m)} 매출" in out.columns
    ]
    current_sales_col = f"{_fmt_yyyymm_col(current_month)} 매출" if current_month else ""

    for c in month_sales_cols + month_qty_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)

    # 월평균 / 최근 평균 / 증감률 / 판정
    month_count = max(len(months), 1)

    if "총매출액" not in out.columns:
        out["총매출액"] = 0

    completed_month_count = len(completed_sales_cols)
    if completed_sales_cols:
        completed_total_sales = out[completed_sales_cols].sum(axis=1)
    else:
        completed_total_sales = pd.Series(0, index=out.index, dtype="float64")

    out["완료월총매출"] = completed_total_sales
    out["완료월수"] = completed_month_count
    out["완료월평균매출"] = completed_total_sales / max(completed_month_count, 1)
    out["월평균매출"] = out["완료월평균매출"]

    if current_sales_col and current_sales_col in out.columns:
        current_sales = pd.to_numeric(out[current_sales_col], errors="coerce").fillna(0)
    else:
        current_sales = pd.Series(0, index=out.index, dtype="float64")

    out["당월 현재매출"] = current_sales

    # 기간 중 실제 매출이 발생한 월수
    # 예측등급에서 자료부족/감소예상을 구분하는 기준으로 사용한다.
    if completed_sales_cols:
        out["매출발생월수"] = out[completed_sales_cols].apply(
            lambda r: int((pd.to_numeric(r, errors="coerce").fillna(0) != 0).sum()),
            axis=1,
        )
    else:
        out["매출발생월수"] = 0


    recent3_cols = completed_sales_cols[-3:] if completed_sales_cols else []
    recent6_cols = completed_sales_cols[-6:] if completed_sales_cols else []

    if recent3_cols:
        out["최근3개월평균매출"] = out[recent3_cols].sum(axis=1) / len(recent3_cols)
    else:
        out["최근3개월평균매출"] = 0

    if recent6_cols:
        out["최근6개월평균매출"] = out[recent6_cols].sum(axis=1) / len(recent6_cols)
    else:
        out["최근6개월평균매출"] = 0

    out["최근3개월증감률"] = out.apply(
        lambda r: _pct_change(r.get("최근3개월평균매출"), r.get("최근6개월평균매출")),
        axis=1,
    )

    out["_has_negative_month"] = False
    if completed_sales_cols:
        out["_has_negative_month"] = out[completed_sales_cols].lt(0).any(axis=1)

    out["추세판정"] = out.apply(
        lambda r: _trend_judge(
            float(completed_total_sales.loc[r.name] if r.name in completed_total_sales.index else 0),
            float(r.get("최근3개월평균매출") or 0),
            float(r.get("최근6개월평균매출") or 0),
            bool(r.get("_has_negative_month")),
        ),
        axis=1,
    )

    if "_has_negative_month" in out.columns:
        out = out.drop(columns=["_has_negative_month"])

    forecast_projection = out.apply(
        lambda r: _forecast_projection_from_row(r),
        axis=1,
        result_type="expand",
    )
    if not forecast_projection.empty:
        forecast_projection.columns = ["_당월예상기준", "_당월적용증감률", "_당월예상매출"]
        out["_당월예상기준"] = forecast_projection["_당월예상기준"]
        out["_당월적용증감률"] = forecast_projection["_당월적용증감률"]
        out["당월 예상매출"] = forecast_projection["_당월예상매출"] if current_month else 0
    else:
        out["_당월예상기준"] = "자료부족"
        out["_당월적용증감률"] = 0
        out["당월 예상매출"] = 0

    monthly_workforward = _build_sales_month_workforward_metrics(df, params)
    if current_month and not monthly_workforward.empty:
        eval_monthly = monthly_workforward[monthly_workforward["기준월"].astype(str) == current_month].copy()
        if not eval_monthly.empty:
            eval_cols = [
                "제품코드",
                "월시점 완료월총매출",
                "월시점 완료월수",
                "월시점 완료월평균매출",
                "월시점 매출발생월수",
                "월시점 최근3개월평균매출",
                "월시점 최근6개월평균매출",
                "월시점 증감률",
                "월시점 추세판정",
                "월시점 실제매출",
                "월시점 예상매출",
                "월시점 잔여예상",
                "월시점 달성률",
            ]
            eval_monthly = eval_monthly[[c for c in eval_cols if c in eval_monthly.columns]].drop_duplicates("제품코드")
            out = out.merge(eval_monthly, on="제품코드", how="left")
            override_pairs = {
                "월시점 완료월총매출": "완료월총매출",
                "월시점 완료월수": "완료월수",
                "월시점 완료월평균매출": "완료월평균매출",
                "월시점 매출발생월수": "매출발생월수",
                "월시점 최근3개월평균매출": "최근3개월평균매출",
                "월시점 최근6개월평균매출": "최근6개월평균매출",
                "월시점 증감률": "최근3개월증감률",
                "월시점 추세판정": "추세판정",
                "월시점 실제매출": "당월 현재매출",
                "월시점 예상매출": "당월 예상매출",
                "월시점 잔여예상": "당월 잔여예상",
                "월시점 달성률": "당월 진척률",
            }
            for src, dst in override_pairs.items():
                if src in out.columns:
                    out[dst] = out[src]
            if "완료월평균매출" in out.columns:
                out["월평균매출"] = out["완료월평균매출"]
            out = out.drop(columns=[c for c in override_pairs if c in out.columns])

    out["당월 잔여예상"] = (out["당월 예상매출"] - out["당월 현재매출"]).clip(lower=0)
    out["당월 진척률"] = out.apply(
        lambda r: (
            float(r.get("당월 현재매출") or 0) / float(r.get("당월 예상매출") or 0) * 100
            if abs(float(r.get("당월 예상매출") or 0)) >= 1e-12
            else 0
        ),
        axis=1,
    )

    round_cols = ["완료월총매출", "월평균매출", "완료월평균매출", "당월 현재매출", "당월 예상매출", "당월 잔여예상"]
    for c in round_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).round(0)
    out = out.drop(
        columns=[c for c in ["_당월예상기준", "_당월적용증감률", "_당월예상매출"] if c in out.columns]
    )
    t_calc = time.perf_counter()

    # 컬럼 순서
    front_cols = [c for c in [
        "제품코드",
        "제품명",
        "규격",
        "제조사코드",
        "제조사명",
        "제품그룹명",
        "제품구분명",
        "제품분류명",
        "총출고수량",
        "총출고할증수량",
        "총매출공급가액",
        "총매출세액",
        "총매출액",
        "완료월총매출",
        "월평균매출",
        "완료월수",
        "완료월평균매출",
        "당월 현재매출",
        "당월 예상매출",
        "당월 잔여예상",
        "당월 진척률",
        "매출발생월수",
        "최근3개월평균매출",
        "최근6개월평균매출",
        "최근3개월증감률",
        "추세판정",
        "거래처수",
        "매입처수",
        "재고적용처수",
        "총출고건수",
        "총집계건수",
    ] if c in out.columns]

    monthly_cols = []
    for m in months:
        s_col = f"{_fmt_yyyymm_col(m)} 매출"
        q_col = f"{_fmt_yyyymm_col(m)} 수량"
        if s_col in out.columns:
            monthly_cols.append(s_col)
        if q_col in out.columns:
            monthly_cols.append(q_col)

    rest_cols = [c for c in out.columns if c not in front_cols + monthly_cols]
    out = out[front_cols + monthly_cols + rest_cols]

    # 기본 정렬: 총매출액 큰 순
    if "총매출액" in out.columns:
        out = out.sort_values(["총매출액", "제품코드"], ascending=[False, True]).reset_index(drop=True)
    else:
        out = out.sort_values(["제품코드"]).reset_index(drop=True)

    out = _normalize_analytics_numeric_columns(out)
    out.attrs.update(getattr(raw, "attrs", {}))
    t_sort = time.perf_counter()
    log.info(
        "[analytics.sales_trend_summary.perf] raw_rows=%s out_rows=%s months=%s raw=%.3fs group=%.3fs pivot_sales=%.3fs pivot_qty=%.3fs merge=%.3fs calc=%.3fs sort=%.3fs total=%.3fs",
        len(raw),
        len(out),
        len(months),
        t_raw - t0,
        t_group - t_raw,
        t_pivot_sales - t_group,
        t_pivot_qty - t_pivot_sales,
        t_merge - t_pivot_qty,
        t_calc - t_merge,
        t_sort - t_calc,
        t_sort - t0,
    )
    return out

def _parse_yyyymm(value: Any) -> str:
    s = _digits_only(value)
    if len(s) >= 6:
        return s[:6]
    return ""


def _iter_yyyymm(month_from: str, month_to: str) -> list[str]:
    mf = _parse_yyyymm(month_from)
    mt = _parse_yyyymm(month_to)

    if not mf or not mt:
        return []

    y = int(mf[:4])
    m = int(mf[4:6])

    y2 = int(mt[:4])
    m2 = int(mt[4:6])

    out: list[str] = []

    while (y < y2) or (y == y2 and m <= m2):
        out.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            y += 1
            m = 1

    return out


def _fmt_yyyymm_col(yyyymm: str) -> str:
    s = _parse_yyyymm(yyyymm)
    if len(s) == 6:
        return f"{s[:4]}-{s[4:6]}"
    return str(yyyymm or "")


def _month_list_for_summary(params: Dict[str, Any], df: pd.DataFrame) -> list[str]:
    months = _iter_yyyymm(params.get("month_from"), params.get("month_to"))
    if months:
        return months

    if isinstance(df, pd.DataFrame) and not df.empty and "기준월" in df.columns:
        vals = []
        for v in df["기준월"].dropna().unique().tolist():
            s = _parse_yyyymm(v)
            if s:
                vals.append(s)
        return sorted(set(vals))

    return []


def _current_yyyymm() -> str:
    try:
        return pd.Timestamp.today().strftime("%Y%m")
    except Exception:
        return dt.datetime.now().strftime("%Y%m")


def _split_sales_period_months(months: list[str], params: Optional[Dict[str, Any]] = None) -> tuple[list[str], str, list[str]]:
    """
    조회월을 완료월/진행 중인 당월/미래월로 분리한다.

    - 조회 종료월이 과거이면 모든 조회월을 완료월로 본다.
    - 현재월은 완료월 평균/추세 계산에서 제외한다.
    - 미래월도 평균/추세 계산에서 제외한다.
    """
    clean_months = [_parse_yyyymm(m) for m in months]
    clean_months = [m for m in clean_months if len(m) == 6]
    if not clean_months:
        return [], "", []

    if params is not None:
        policy = _resolve_period_source_policy(params)
        evaluation_month = str(policy.get("evaluation_month") or "")
    else:
        evaluation_month = _current_yyyymm()

    if not evaluation_month:
        return clean_months, "", []

    completed = [m for m in clean_months if m < evaluation_month]
    current = evaluation_month if evaluation_month in clean_months else ""
    future = [m for m in clean_months if m > evaluation_month]
    return completed, current, future


def _trend_judge(total_sales: float, recent3: float, recent6: float, has_negative_month: bool) -> str:
    """
    품목별 추세판정.

    핵심 보정:
    - 총매출이 있는데 최근 3/6개월이 모두 0이면 '자료부족'이 아니라 '감소'로 본다.
    - 총매출 자체가 없고 최근값도 없을 때만 '자료부족'으로 본다.
    """
    total_sales = float(total_sales or 0)
    recent3 = float(recent3 or 0)
    recent6 = float(recent6 or 0)

    if has_negative_month or total_sales < 0:
        return "반품주의"

    # 기간 내 매출 자체가 없으면 자료부족
    if total_sales <= 0 and recent6 <= 0 and recent3 <= 0:
        return "자료부족"

    # 과거 매출은 있는데 최근 3/6개월 모두 없으면 감소
    if total_sales > 0 and recent6 <= 0 and recent3 <= 0:
        return "감소"

    if recent6 <= 0 and recent3 > 0:
        return "신규/증가"

    ratio = recent3 / recent6 if recent6 else 0

    if ratio >= 1.15:
        return "증가"
    if ratio <= 0.85:
        return "감소"

    return "안정"

def _pct_change(recent3: float, recent6: float) -> float:
    try:
        if abs(float(recent6 or 0)) < 1e-12:
            return 0
        return ((float(recent3 or 0) - float(recent6 or 0)) / float(recent6 or 0)) * 100
    except Exception:
        return 0

def _fmt_num_for_summary(value: Any) -> str:
    try:
        v = float(value or 0)
        if v.is_integer():
            return f"{int(v):,}"
        return f"{v:,.2f}"
    except Exception:
        return "0"

def _summary_meta(df: pd.DataFrame) -> Dict[str, Any]:
    if df is None or df.empty:
        return {
            "row_count": 0,
            "row_count_total": 0,
            "sum_qty": 0,
            "sum_sales_amt": 0,
            "product_count": 0,
            "customer_count": 0,
            "month_count": 0,
        }

    out = df.copy()

    def _sum(col: str) -> float:
        if col not in out.columns:
            return 0
        return float(pd.to_numeric(out[col], errors="coerce").fillna(0).sum())

    if "거래처코드" in out.columns:
        customer_label = "거래처수"
    elif "매입처코드" in out.columns:
        customer_label = "매입처수"
    elif "재고적용처코드" in out.columns:
        customer_label = "재고적용처수"
    else:
        customer_label = "거래처수"


    return {
        "row_count": int(len(out)),
        "row_count_total": int(len(out)),
        "sum_qty": _sum("출고수량"),
        "sum_sales_amt": _sum("매출합계"),
        "sum_supply_amt": _sum("매출공급가액"),
        "sum_tax_amt": _sum("매출세액"),
        "product_count": int(out["제품코드"].nunique()) if "제품코드" in out.columns else 0,
        "customer_count": (
            int(out["거래처코드"].nunique())
            if "거래처코드" in out.columns
            else int(out["매입처코드"].nunique())
            if "매입처코드" in out.columns
            else int(out["재고적용처코드"].nunique())
            if "재고적용처코드" in out.columns
            else 0
        ),
        "customer_count_label": customer_label,
        "month_count": int(out["기준월"].nunique()) if "기준월" in out.columns else 0,
    }


def _period_policy_meta_from_summary_df(df: pd.DataFrame) -> Dict[str, Any]:
    if df is None or df.empty:
        return {
            "completed_month_count": 0,
            "sum_completed_month_sales_amt": 0,
            "avg_completed_month_sales_amt": 0,
            "sum_current_month_sales_amt": 0,
            "sum_current_month_expected_amt": 0,
            "sum_current_month_remaining_expected_amt": 0,
            "current_month_progress_pct": 0,
        }

    out = df.copy()

    def _sum(col: str) -> float:
        if col not in out.columns:
            return 0.0
        return float(pd.to_numeric(out[col], errors="coerce").fillna(0).sum())

    completed_month_count = 0
    if "완료월수" in out.columns:
        try:
            completed_month_count = int(pd.to_numeric(out["완료월수"], errors="coerce").fillna(0).max())
        except Exception:
            completed_month_count = 0

    sum_completed_sales = _sum("완료월총매출")
    sum_current_sales = _sum("당월 현재매출")
    sum_current_expected = _sum("당월 예상매출")
    current_progress = (
        sum_current_sales / sum_current_expected * 100
        if abs(sum_current_expected) >= 1e-12
        else 0
    )

    return {
        "completed_month_count": completed_month_count,
        "sum_completed_month_sales_amt": sum_completed_sales,
        "avg_completed_month_sales_amt": (
            sum_completed_sales / completed_month_count if completed_month_count > 0 else 0
        ),
        "sum_current_month_sales_amt": sum_current_sales,
        "sum_current_month_expected_amt": sum_current_expected,
        "sum_current_month_remaining_expected_amt": _sum("당월 잔여예상"),
        "current_month_progress_pct": current_progress,
    }


def get_sales_trend_result(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = coalesce_params(params)
    df = get_sales_trend_df(params)
    df = _ensure_analysis_seq_column(df, mode="product")

    try:
        summary_for_counts = get_sales_trend_summary_df(params, raw_df=df)
        trend_judge_counts = _trend_judge_counts(summary_for_counts)

        trend_judge_filter = _normalize_trend_judge_filter(params.get("trend_judge"))
        if trend_judge_filter and isinstance(summary_for_counts, pd.DataFrame) and not summary_for_counts.empty:
            if "제품코드" in summary_for_counts.columns and "추세판정" in summary_for_counts.columns:
                product_codes = (
                    summary_for_counts[
                        summary_for_counts["추세판정"].fillna("").astype(str).str.strip() == trend_judge_filter
                    ]["제품코드"]
                    .fillna("")
                    .astype(str)
                    .str.strip()
                    .tolist()
                )
                product_codes = [x for x in product_codes if x]

                if product_codes and isinstance(df, pd.DataFrame) and "제품코드" in df.columns:
                    df = df[df["제품코드"].fillna("").astype(str).str.strip().isin(product_codes)].copy()
                    df = df.reset_index(drop=True)
                elif isinstance(df, pd.DataFrame):
                    df = df.iloc[0:0].copy()
    except Exception:
        log.exception("[analytics.sales_trend] failed to build trend_judge_counts")
        trend_judge_counts = {}

    trend_judge_filter = _normalize_trend_judge_filter(params.get("trend_judge"))

    # 월별 원자료 df에도 제품별 추세판정을 붙여서
    # 화면 표에서 필터 적용 여부를 확인할 수 있게 한다.
    if (
        isinstance(summary_for_counts, pd.DataFrame)
        and not summary_for_counts.empty
        and "제품코드" in summary_for_counts.columns
        and "추세판정" in summary_for_counts.columns
        and isinstance(df, pd.DataFrame)
        and not df.empty
        and "제품코드" in df.columns
    ):
        judge_map_df = summary_for_counts[["제품코드", "추세판정"]].copy()
        judge_map_df["제품코드"] = judge_map_df["제품코드"].fillna("").astype(str).str.strip()
        judge_map_df["추세판정"] = judge_map_df["추세판정"].fillna("").astype(str).str.strip()
        judge_map_df = judge_map_df[judge_map_df["제품코드"] != ""]
        judge_map_df = judge_map_df.drop_duplicates(subset=["제품코드"], keep="first")

        judge_map = dict(zip(judge_map_df["제품코드"], judge_map_df["추세판정"]))

        df = df.copy()
        mapped = df["제품코드"].fillna("").astype(str).str.strip().map(judge_map).fillna("")

        if "추세판정" not in df.columns:
            df["추세판정"] = mapped
        else:
            cur = df["추세판정"].fillna("").astype(str).str.strip()
            df["추세판정"] = cur
            df.loc[df["추세판정"] == "", "추세판정"] = mapped

    row_count = 0 if df is None else int(len(df))

    log.info("[analytics.sales_trend] rows=%s params=%r", row_count, params)

    source_mode = _resolve_source_mode(params)
    source_label = _effective_source_label(source_mode, df)
    query_summary = _fmt_analytics_query_summary(params, source_label)
    trend_judge_filter = _normalize_trend_judge_filter(params.get("trend_judge"))    

    if row_count == 0:
        return {
            "table": TABLE,
            "title": "품목별 매출 추세 분석",
            "action": "품목별 매출 추세 분석",
            "params": params,
            "data": "해당 조회조건의 자료가 없습니다.",
            "message": "해당 조회조건의 자료가 없습니다.",
            "final": True,
            "type": "text",
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "analytics": True,
                "analysis_type": "sales_trend",
                "source_mode": source_mode,
                "source_label": source_label,
                "trend_judge_counts": trend_judge_counts,
                "query_summary": query_summary,
                "condition": query_summary,
                "summary_md": (
                    f"조회조건: {query_summary}\n\n"
                    f"매출추세분석: 해당 조회조건의 자료가 없습니다. / "
                    f"자료원 {source_label}"
                ),
            },            
        }
    
    if isinstance(df, pd.DataFrame) and "추세판정" in df.columns and "판정결과" not in df.columns:
        insert_at = list(df.columns).index("추세판정") + 1
        df.insert(insert_at, "판정결과", df["추세판정"])

    payload = build_result_payload(
        table=TABLE,
        title="품목별 매출 추세 분석",
        action="품목별 매출 추세 분석",
        params=params,
        df=df,
        message=f"품목별 매출 추세 분석 {row_count:,}건",
    )

    meta = dict(payload.get("meta") or {})
    meta.update(_summary_meta(df))
    meta.update(_period_policy_meta_from_summary_df(summary_for_counts))

    meta.update({
        "analytics": True,
        "analysis_type": "sales_trend",
        "source_mode": source_mode,
        "source_label": source_label,
        "trend_judge_counts": trend_judge_counts,
        "trend_judge_filter": trend_judge_filter,
        "query_summary": _fmt_analytics_query_summary(params, source_label),
        "condition": _fmt_analytics_query_summary(params, source_label),                
        "summary_md": (
            f"매출추세분석: "
            f"조회조건 {query_summary} / "
            f"총매출액 {_fmt_num_for_summary(meta.get('sum_sales_amt'))} / "
            f"출고수량 {_fmt_num_for_summary(meta.get('sum_qty'))} / "
            f"품목수 {_fmt_num_for_summary(meta.get('product_count'))} / "
            f"{meta.get('customer_count_label') or '거래처수'} {_fmt_num_for_summary(meta.get('customer_count'))} / "
            f"분석월수 {_fmt_num_for_summary(meta.get('month_count'))} / "
            f"{'현재 조회대상 추세판정 ' + trend_judge_filter + ' / ' if trend_judge_filter else ''}"
            f"전체 추세판정 분포 {_fmt_counts_for_summary(trend_judge_counts)} / "
            f"자료원 {source_label}"
        ),
    })
    payload["meta"] = meta

    # 최종 안전 보정: 회귀 테스트와 화면 헤더에서 사용하는 추세판정별 제품수
    payload.setdefault("meta", {})
    payload["meta"]["trend_judge_counts"] = trend_judge_counts

    return payload

def _trend_judge_counts(summary_df: pd.DataFrame) -> Dict[str, int]:
    return _count_text_values(summary_df, "추세판정")

# 요약표는 원자료 기준으로 계산해야 총매출/총수량이 정확하다.
# 따라서 summary_df가 아니라 raw_df를 사용한다.
def _forecast_grade_counts(df: pd.DataFrame) -> Dict[str, int]:
    return _count_text_values(df, "예상등급")

TREND_JUDGE_ORDER = ["감소", "안정", "증가", "신규/증가", "자료부족", "반품주의"]


def _normalize_trend_judge_filter(value: Any) -> str:
    s = clean_text(value)
    if not s:
        return ""

    s = s.replace(" ", "")

    aliases = {
        "감소": "감소",
        "감소품목": "감소",
        "감소추세": "감소",
        "안정": "안정",
        "안정품목": "안정",
        "증가": "증가",
        "증가품목": "증가",
        "증가추세": "증가",
        "신규증가": "신규/증가",
        "신규/증가": "신규/증가",
        "자료부족": "자료부족",
        "데이터부족": "자료부족",
        "반품주의": "반품주의",
    }

    return aliases.get(s, "")


def _fmt_counts_for_summary(counts: Dict[str, int]) -> str:
    if not isinstance(counts, dict) or not counts:
        return "없음"

    keys = [k for k in TREND_JUDGE_ORDER if k in counts]
    keys += [k for k in counts.keys() if k not in keys]

    return ", ".join(f"{k} {int(counts.get(k) or 0):,}개" for k in keys)


def _apply_trend_judge_filter(df: pd.DataFrame, params: Dict[str, Any]) -> pd.DataFrame:
    judge = _normalize_trend_judge_filter(params.get("trend_judge"))

    if not judge:
        return df

    if df is None or df.empty or "추세판정" not in df.columns:
        return df

    out = df[df["추세판정"].fillna("").astype(str).str.strip() == judge].copy()
    return out.reset_index(drop=True)

def _filter_raw_df_by_product_df(raw_df: pd.DataFrame, product_df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(raw_df, pd.DataFrame) or raw_df.empty:
        return pd.DataFrame()

    if not isinstance(product_df, pd.DataFrame) or product_df.empty:
        return raw_df.iloc[0:0].copy()

    if "제품코드" not in raw_df.columns or "제품코드" not in product_df.columns:
        return raw_df

    codes = (
        product_df["제품코드"]
        .fillna("")
        .astype(str)
        .str.strip()
        .tolist()
    )
    codes = {x for x in codes if x}

    if not codes:
        return raw_df.iloc[0:0].copy()

    out = raw_df[
        raw_df["제품코드"].fillna("").astype(str).str.strip().isin(codes)
    ].copy()

    return out.reset_index(drop=True)


def _fmt_analytics_query_summary(params: Dict[str, Any], source_label: str = "") -> str:
    bits: list[str] = []

    date_from = _digits_only(params.get("date_from"))
    date_to = _digits_only(params.get("date_to"))

    def _fmt_date(s: str) -> str:
        if len(s) >= 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        return s

    if date_from and date_to and date_from != date_to:
        bits.append(f"기간 {_fmt_date(date_from)} ~ {_fmt_date(date_to)}")
    elif date_from:
        bits.append(f"기간 {_fmt_date(date_from)}")
    elif date_to:
        bits.append(f"기간 {_fmt_date(date_to)}")

    if source_label:
        bits.append(f"분석자료원 {source_label}")

    if clean_text(params.get("sido_nm")):
        bits.append(f"시도명 {params.get('sido_nm')}")
    if clean_text(params.get("gugun_nm")):
        bits.append(f"시구군명 {params.get('gugun_nm')}")
    if clean_text(params.get("road_nm")):
        bits.append(f"도로명 {params.get('road_nm')}")

    trend_judge = _normalize_trend_judge_filter(params.get("trend_judge"))
    if trend_judge:
        bits.append(f"추세판정 {trend_judge}")

    return " / ".join(bits)

# 요약표 결과. 총매출/총수량/품목수/거래처수/분석월수 등.
# trend_judge_counts와 forecast_grade_counts는 판정/등급별 품목수.
# summary_md는 요약표 상단에 표시할 간략한 텍스트. trend_judge_counts와 forecast_grade_counts를 함께 보여준다.
def get_sales_trend_summary_result(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = coalesce_params(params)
    params = _apply_month_or_date_params(params)

    # meta는 원자료 기준으로 계산해야 총매출/총수량이 정확하다.
    raw_df = get_sales_trend_df(params)
    summary_df = get_sales_trend_summary_df(params, raw_df=raw_df)

    # 전체 추세판정 분포는 필터 전 요약표 기준
    trend_counts = _trend_judge_counts(summary_df)
    trend_judge_filter = _normalize_trend_judge_filter(params.get("trend_judge"))
    if trend_judge_filter:
        summary_df = _apply_trend_judge_filter(summary_df, params)

    # 필터된 제품 기준으로 원자료도 같이 줄여야 금액/수량/거래처수가 맞는다.
    raw_for_meta = _filter_raw_df_by_product_df(raw_df, summary_df)

    summary_df = _ensure_analysis_seq_column(summary_df, mode="row")
    row_count = 0 if summary_df is None else int(len(summary_df))

    log.info("[analytics.sales_trend_summary] rows=%s params=%r", row_count, params)

    source_mode = _resolve_source_mode(params)
    source_label = _effective_source_label(source_mode, raw_df)
    query_summary = _fmt_analytics_query_summary(params, source_label)

    if row_count == 0:
        return {
            "table": TABLE,
            "title": "품목별 매출 추세 요약표",
            "action": "품목별 매출 추세 요약표",
            "params": params,
            "data": "해당 조회조건의 자료가 없습니다.",
            "message": "해당 조회조건의 자료가 없습니다.",
            "final": True,
            "type": "text",
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "analytics": True,
                "analysis_type": "sales_trend",
                "sales_trend_summary": True,
                "summary_type": "product_summary",
                "source_mode": source_mode,
                "source_label": source_label,
                "trend_judge_counts": trend_counts,
                "trend_judge_filter": trend_judge_filter,
                "query_summary": query_summary,
                "condition": query_summary,
            },
        }

    payload = build_result_payload(
        table=TABLE,
        title="품목별 매출 추세 요약표",
        action="품목별 매출 추세 요약표",
        params=params,
        df=summary_df,
        message=f"품목별 매출 추세 요약표 {row_count:,}건",
    )

    meta = dict(payload.get("meta") or {})
    meta.update(_summary_meta(raw_for_meta))
    meta.update(_period_policy_meta_from_summary_df(summary_df))

    meta.update({
        "row_count": int(row_count),
        "row_count_total": int(row_count),
        "analytics": True,
        "analysis_type": "sales_trend",
        "sales_trend_summary": True,
        "summary_type": "product_summary",
        "source_mode": source_mode,
        "source_label": source_label,
        "trend_judge_counts": trend_counts,
        "trend_judge_filter": trend_judge_filter,
        "query_summary": query_summary,
        "condition": query_summary,
        "summary_md": (
            f"매출추세요약: "
            f"조회조건 {query_summary} / "
            f"총매출액 {_fmt_num_for_summary(meta.get('sum_sales_amt'))} / "
            f"출고수량 {_fmt_num_for_summary(meta.get('sum_qty'))} / "
            f"품목수 {_fmt_num_for_summary(row_count)} / "
            f"거래처수 {_fmt_num_for_summary(meta.get('customer_count'))} / "
            f"분석월수 {_fmt_num_for_summary(meta.get('month_count'))} / "
            f"{'현재 조회대상 추세판정 ' + trend_judge_filter + ' / ' if trend_judge_filter else ''}"
            f"전체 추세판정 분포 {_fmt_counts_for_summary(trend_counts)} / "
            f"자료원 {source_label}"
        ),        
    })

    payload["meta"] = meta
    return payload

# 유틸리티 함수
def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0

# 값이 너무 크거나 작은 경우, 또는 비율 계산에서 분모가 0인 경우 등을 방지하기 위한 클램프 함수
def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _forecast_base_and_label(row: pd.Series) -> tuple[float, str]:
    """
    예상 기준값 선택.
    우선순위:
    1. 최근3개월평균매출
    2. 최근6개월평균매출
    3. 완료월평균매출
    4. 월평균매출
    """
    recent3 = _safe_float(row.get("최근3개월평균매출"))
    recent6 = _safe_float(row.get("최근6개월평균매출"))
    completed_avg = _safe_float(row.get("완료월평균매출"))
    avg = _safe_float(row.get("월평균매출"))

    if recent3 > 0:
        return recent3, "최근3개월평균매출"
    if recent6 > 0:
        return recent6, "최근6개월평균매출"
    if completed_avg > 0:
        return completed_avg, "완료월평균매출"
    if avg > 0:
        return avg, "월평균매출"

    return 0.0, "자료부족"


def _forecast_grade(row: pd.Series) -> str:
    """
    품목별 매출 예상등급.

    추세판정과 1:1 매핑하지 않고,
    매출발생월수 / 최근3개월 / 최근6개월 / 증감률을 함께 본다.
    """
    judge = str(row.get("추세판정") or "").strip()

    total_sales = _safe_float(row.get("완료월총매출"))
    if total_sales <= 0:
        total_sales = _safe_float(row.get("총매출액"))
    avg_sales = _safe_float(row.get("완료월평균매출")) or _safe_float(row.get("월평균매출"))
    recent3 = _safe_float(row.get("최근3개월평균매출"))
    recent6 = _safe_float(row.get("최근6개월평균매출"))
    rate = _safe_float(row.get("최근3개월증감률"))
    active_months = int(_safe_float(row.get("매출발생월수")))

    # 반품은 예측에서도 별도 주의
    if judge == "반품주의" or total_sales < 0:
        return "반품주의"

    # 기간 전체에 매출이 없으면 예측 불가
    if total_sales <= 0:
        return "자료부족"

    # 매출발생월이 너무 적으면 감소/상승으로 단정하지 않는다.
    if active_months <= 1:
        return "자료부족"

    # 최근 6개월은 없고 최근 3개월만 있으면 신규/재상승 후보
    if recent6 <= 0 and recent3 > 0:
        return "신규확인"

    # 최근 6개월 평균은 있는데 최근 3개월이 0이면 확실한 감소
    if recent6 > 0 and recent3 <= 0:
        return "감소예상"

    # 최근3개월 증감률 기준
    if rate >= 20:
        return "상승예상"
    if rate <= -20:
        return "감소예상"

    # 평균 대비 최근 흐름 보조판정
    if avg_sales > 0:
        if recent3 >= avg_sales * 1.15:
            return "상승예상"
        if recent3 <= avg_sales * 0.75:
            return "감소예상"

    return "안정예상"


def _forecast_projection_from_row(row: pd.Series) -> tuple[str, float, float]:
    base, base_label = _forecast_base_and_label(row)

    raw_rate = _safe_float(row.get("최근3개월증감률")) / 100.0
    safe_rate = _clamp(raw_rate, -0.30, 0.30)
    adjusted_rate = safe_rate * 0.5

    grade = _forecast_grade(row)
    if grade in {"반품주의", "자료부족"}:
        next_month = max(base, 0)
    else:
        next_month = max(base * (1.0 + adjusted_rate), 0)

    return base_label, adjusted_rate * 100, next_month


def _forecast_meta_from_df(df: pd.DataFrame) -> Dict[str, Any]:
    if df is None or df.empty:
        return {
            "row_count": 0,
            "row_count_total": 0,
            "sum_qty": 0,
            "sum_sales_amt": 0,
            "sum_supply_amt": 0,
            "sum_tax_amt": 0,
            "product_count": 0,
            "customer_count": 0,
            "customer_count_label": "거래처수",
            "month_count": 0,
        }

    out = df.copy()

    def _sum(col: str) -> float:
        if col not in out.columns:
            return 0.0
        return float(pd.to_numeric(out[col], errors="coerce").fillna(0).sum())

    month_sales_cols = [
        c for c in out.columns
        if isinstance(c, str) and c.endswith(" 매출") and c[:4].isdigit()
    ]

    if "거래처수" in out.columns:
        customer_count = _sum("거래처수")
        customer_label = "거래처수합계"
    elif "매입처수" in out.columns:
        customer_count = _sum("매입처수")
        customer_label = "매입처수합계"
    elif "재고적용처수" in out.columns:
        customer_count = _sum("재고적용처수")
        customer_label = "재고적용처수합계"
    else:
        customer_count = 0
        customer_label = "거래처수"

    return {
        "row_count": int(len(out)),
        "row_count_total": int(len(out)),
        "sum_qty": _sum("총출고수량"),
        "sum_sales_amt": _sum("총매출액"),
        "sum_supply_amt": _sum("총매출공급가액"),
        "sum_tax_amt": _sum("총매출세액"),
        "product_count": int(out["제품코드"].nunique()) if "제품코드" in out.columns else int(len(out)),
        "customer_count": customer_count,
        "customer_count_label": customer_label,
        "month_count": int(len(month_sales_cols)),
    }


def get_sales_forecast_df(
    params: Optional[Dict[str, Any]] = None,
    raw_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    품목별 매출 예상.
    품목별 매출 추세 요약표를 기반으로 예상값을 계산한다.
    """
    t0 = time.perf_counter()
    params = coalesce_params(params)
    params = _apply_month_or_date_params(params)

    df = get_sales_trend_summary_df(params, raw_df=raw_df)
    t_summary = time.perf_counter()
    if df is None or df.empty:
        log.info(
            "[analytics.sales_forecast.perf] rows=0 summary=%.3fs numeric=0.000s calc=0.000s finish=0.000s total=%.3fs",
            t_summary - t0,
            time.perf_counter() - t0,
        )
        return pd.DataFrame()

    out = df.copy()

    required_num_cols = [
        "총매출액",
        "완료월총매출",
        "월평균매출",
        "완료월수",
        "완료월평균매출",
        "당월 현재매출",
        "당월 예상매출",
        "당월 잔여예상",
        "당월 진척률",
        "매출발생월수",
        "최근3개월평균매출",
        "최근6개월평균매출",
        "최근3개월증감률",
        "총출고수량",
    ]

    for c in required_num_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)
    t_numeric = time.perf_counter()

    forecast_rows = []

    for _, row in out.iterrows():
        base_label, adjusted_rate_pct, next_month = _forecast_projection_from_row(row)
        grade = _forecast_grade(row)

        forecast_rows.append({
            "예상기준": base_label,
            "적용증감률": adjusted_rate_pct,
            "다음월예상매출": next_month,
            "3개월예상매출": next_month * 3,
            "6개월예상매출": next_month * 6,
            "예상등급": grade,
        })

    forecast_df = pd.DataFrame(forecast_rows)
    out = pd.concat([out.reset_index(drop=True), forecast_df], axis=1)
    for c in ["다음월예상매출", "3개월예상매출", "6개월예상매출"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).round(0)
    t_calc = time.perf_counter()

    front_cols = [c for c in [
        "제품코드",
        "제품명",
        "규격",
        "제조사코드",
        "제조사명",
        "제품그룹명",
        "제품구분명",
        "제품분류명",
        "총출고수량",
        "총출고할증수량",
        "총매출공급가액",
        "총매출세액",
        "총매출액",
        "완료월총매출",
        "월평균매출",
        "완료월수",
        "완료월평균매출",
        "당월 현재매출",
        "당월 예상매출",
        "당월 잔여예상",
        "당월 진척률",
        "매출발생월수",
        "최근3개월평균매출",
        "최근6개월평균매출",
        "최근3개월증감률",
        "추세판정",
        "예상기준",
        "적용증감률",
        "다음월예상매출",
        "3개월예상매출",
        "6개월예상매출",
        "예상등급",
        "거래처수",
        "매입처수",
        "재고적용처수",
        "총출고건수",
        "총집계건수",
    ] if c in out.columns]

    monthly_cols = [
        c for c in out.columns
        if isinstance(c, str)
        and (c.endswith(" 매출") or c.endswith(" 수량"))
        and c not in front_cols
    ]

    rest_cols = [c for c in out.columns if c not in front_cols + monthly_cols]
    out = out[front_cols + monthly_cols + rest_cols]

    if "다음월예상매출" in out.columns:
        out = out.sort_values(
            ["다음월예상매출", "제품코드"],
            ascending=[False, True],
        ).reset_index(drop=True)
    else:
        out = out.reset_index(drop=True)

    if "순번" in out.columns:
        out = out.drop(columns=["순번"])

    out.insert(0, "순번", range(1, len(out) + 1))

    out = _normalize_analytics_numeric_columns(out)
    out.attrs.update(getattr(df, "attrs", {}))
    t_finish = time.perf_counter()
    log.info(
        "[analytics.sales_forecast.perf] rows=%s summary=%.3fs numeric=%.3fs calc=%.3fs finish=%.3fs total=%.3fs",
        len(out),
        t_summary - t0,
        t_numeric - t_summary,
        t_calc - t_numeric,
        t_finish - t_calc,
        t_finish - t0,
    )
    return out


def get_sales_forecast_result(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    품목별 매출 예상 결과 빌드.
    """
    params = coalesce_params(params)
    params = _apply_month_or_date_params(params)

    df = get_sales_forecast_df(params)
    if df is None:
        df = pd.DataFrame()

    source_mode = _resolve_source_mode(params)
    source_label = _effective_source_label(source_mode, df)
    trend_judge_filter = _normalize_trend_judge_filter(params.get("trend_judge"))
    query_summary = _fmt_analytics_query_summary(params, source_label)

    # 필터 전 전체 추세판정 분포
    trend_counts_all = _trend_judge_counts(df)

    # 추세판정 필터 적용
    if trend_judge_filter:
        df = _apply_trend_judge_filter(df, params)

    df = _ensure_analysis_seq_column(df, mode="row")
    row_count = 0 if df is None else int(len(df))

    log.info("[analytics.sales_forecast] rows=%s params=%r", row_count, params)

    trend_counts_filtered = _trend_judge_counts(df)
    forecast_counts = _forecast_grade_counts(df)

    sum_next_forecast = _sum_numeric(df, "다음월예상매출")
    sum_3m_forecast = _sum_numeric(df, "3개월예상매출")
    sum_6m_forecast = _sum_numeric(df, "6개월예상매출")

    if row_count == 0:
        return {
            "table": TABLE,
            "title": "품목별 매출 예상",
            "action": "품목별 매출 예상",
            "params": params,
            "data": "해당 조회조건의 자료가 없습니다.",
            "message": "해당 조회조건의 자료가 없습니다.",
            "final": True,
            "type": "text",
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "analytics": True,
                "analysis_type": "sales_forecast",
                "sales_trend_summary": True,
                "summary_type": "product_forecast",
                "source_mode": source_mode,
                "source_label": source_label,
                "trend_judge_counts": {},
                "trend_judge_counts_all": trend_counts_all,
                "trend_judge_filter": trend_judge_filter,
                "forecast_grade_counts": {},
                "sum_next_month_forecast_amt": 0,
                "sum_3month_forecast_amt": 0,
                "sum_6month_forecast_amt": 0,
                "query_summary": query_summary,
                "condition": query_summary,
                "summary_md": (
                    f"매출예상요약: 해당 자료가 없습니다. / "
                    f"조회조건 {query_summary} / "
                    f"자료원 {source_label}"
                ),
            },
        }

    payload = build_result_payload(
        table=TABLE,
        title="품목별 매출 예상",
        action="품목별 매출 예상",
        params=params,
        df=df,
        message=f"품목별 매출 예상 {row_count:,}건",
    )

    meta = dict(payload.get("meta") or {})
    meta.update(_forecast_meta_from_df(df))
    meta.update(_period_policy_meta_from_summary_df(df))

    meta.update({
        "row_count": int(row_count),
        "row_count_total": int(row_count),
        "analytics": True,
        "analysis_type": "sales_forecast",
        "sales_trend_summary": True,
        "summary_type": "product_forecast",
        "source_mode": source_mode,
        "source_label": source_label,

        # 현재 조회대상 기준
        "trend_judge_counts": trend_counts_filtered,
        "forecast_grade_counts": forecast_counts,

        # 전체 분포 참고용
        "trend_judge_counts_all": trend_counts_all,
        "trend_judge_filter": trend_judge_filter,

        "sum_next_month_forecast_amt": sum_next_forecast,
        "sum_3month_forecast_amt": sum_3m_forecast,
        "sum_6month_forecast_amt": sum_6m_forecast,

        "query_summary": query_summary,
        "condition": query_summary,
        "summary_md": (
            f"매출예상요약: "
            f"조회조건 {query_summary} / "
            f"총매출액 {_fmt_num_for_summary(meta.get('sum_sales_amt'))} / "
            f"다음월예상매출 {_fmt_num_for_summary(sum_next_forecast)} / "
            f"3개월예상매출 {_fmt_num_for_summary(sum_3m_forecast)} / "
            f"6개월예상매출 {_fmt_num_for_summary(sum_6m_forecast)} / "
            f"품목수 {_fmt_num_for_summary(row_count)} / "
            f"{'현재 조회대상 추세판정 ' + trend_judge_filter + ' / ' if trend_judge_filter else ''}"
            f"현재 조회대상 추세판정 분포 {_fmt_counts_for_summary(trend_counts_filtered)} / "
            f"전체 추세판정 분포 {_fmt_counts_for_summary(trend_counts_all)} / "
            f"예상등급 분포 {_fmt_counts_for_summary(forecast_counts)} / "
            f"자료원 {source_label}"
        ),
    })

    payload["meta"] = meta
    return payload

def _stock_mode_label(stock_mode: str) -> str:
    return STOCK_MODE_LABELS.get(str(stock_mode or "").strip(), "장부재고")


def _stock_qty_col(stock_mode: str) -> str:
    return "실재고수량" if str(stock_mode or "").strip() == "real" else "장부재고수량"


def _stock_amt_col(stock_mode: str) -> str:
    return "실재고금액" if str(stock_mode or "").strip() == "real" else "장부재고금액"


def _stock_shortage_grade(row: pd.Series) -> str:
    """
    품목별 재고부족 등급 v1.

    기준:
    - 예상기준월수량이 없으면 수요 없음/관찰로 분리
    - 수요가 있는데 재고가 없으면 재고없음
    - 이후는 재고커버월수 기준으로 판정

    재고커버월수:
    - 현재재고수량 / 예상기준월수량
    """
    stock_qty = _safe_float(row.get("현재재고수량"))
    avg_qty = _safe_float(row.get("예상기준월수량"))
    cover_months = _safe_float(row.get("재고커버월수"))

    # 수요 자체가 없는 경우
    if avg_qty <= 0:
        if stock_qty <= 0:
            return "재고없음/수요없음"
        return "수요관찰"

    # 수요는 있는데 재고가 없는 경우
    if stock_qty <= 0:
        return "재고없음"

    # 재고커버월수 기준
    if cover_months < 1:
        return "1개월내 부족"

    if cover_months < 2:
        return "2개월내 부족"

    if cover_months < 3:
        return "3개월내 부족"

    if cover_months <= 3:
        return "3개월내 부족주의"

    return "정상"

def _stock_shortage_current_judge(row: pd.Series) -> str:
    shortage_qty = _safe_float(row.get("부족예상수량"))
    remaining_qty = _safe_float(row.get("당월 잔여예상출고수량"))
    fill_rate = _safe_float(row.get("당월 재고충족률"))

    if shortage_qty > 0:
        return "부족"
    if remaining_qty <= 0:
        return "수요없음"
    if fill_rate < 120:
        return "주의"
    return "적정"

def _shortage_grade_counts(df: pd.DataFrame) -> Dict[str, int]:
    return _count_text_values(df, "부족등급")

# 현재 재고 수량과 금액을 Rddbc040에서 가져온다. 제품코드 기준으로.
# 제품코드가 너무 많으면 SQL Server의 IN 절 파라미터 제한에 걸릴 수 있으므로, 최대 2000개까지만 처리한다.
# 반환값 컬럼: 제품코드, 장부재고수량, 실재고수량, 장부재고단가, 실재고단가
# 제품코드가 없는 경우나 조회 결과가 없는 경우는 빈 데이터프레임을 반환한다.
# backup (pre_enter_to_submit_false_master_analytics_20260505_223546)

def _stock_current_cutoff_month(params: Dict[str, Any]) -> str:
    """
    현재고 산정 기준월.

    재고부족현황의 현재고는 조회 기간의 시작월과 무관하게,
    종료월까지의 월집계 누계로 계산한다.

    예:
      20250101~20260531 조회
      → 현재고는 202605까지 전체 누계
    """
    month_to = _normalize_month(params.get("month_to"))
    if month_to:
        return month_to

    date_to = _normalize_date_to(params.get("date_to"))
    if date_to:
        return date_to[:6]

    return pd.Timestamp.today().strftime("%Y%m")


def _stock_current_monthly_spec(stock_mode: str) -> Dict[str, str]:
    """
    재고부족현황 현재고 산정용 월집계 테이블 spec.
    source_mode와 별개로 stock_mode만 보고 결정한다.
    """
    if str(stock_mode or "").strip() == "real":
        return {
            "table": "dbo.Rddbc210",
            "prefix": "Rd21",
            "qty_col": "실재고수량",
            "amt_col": "실재고금액",
            "unit_col": "실재고평가단가",
            "fallback_unit_col": "Rd04_In_Real_Unit_Cost",
            "source_table": "Rddbc210",
            "source_label": "실재고월집계(Rddbc210) 누계",
        }

    return {
        "table": "dbo.Rddbc220",
        "prefix": "Rd22",
        "qty_col": "장부재고수량",
        "amt_col": "장부재고금액",
        "unit_col": "장부재고평가단가",
        "fallback_unit_col": "Rd04_In_Acc_Unit_Cost",
        "source_table": "Rddbc220",
        "source_label": "장부재고월집계(Rddbc220) 누계",
    }


def _stock_shortage_source_labels(
    params: Optional[Dict[str, Any]] = None,
    *,
    stock_mode: str = "book",
) -> Dict[str, Any]:
    policy = _resolve_period_source_policy(params)
    monthly_source = (
        "월집계-실재고(Rddbc210)"
        if str(stock_mode or "").strip() == "real"
        else "월집계-장부재고(Rddbc220)"
    )
    monthly_stock_source = (
        "실재고월집계(Rddbc210) 누계"
        if str(stock_mode or "").strip() == "real"
        else "장부재고월집계(Rddbc220) 누계"
    )

    if bool(policy.get("use_hybrid_detail")):
        display_source = (
            f"완료월: {monthly_source} / "
            "평가월: 출고상세(Rddbc120) / "
            "현재재고: 전월말+입출고상세"
        )
        stock_source = "전월말재고 + 평가월 입고상세(Rddbc110) - 출고상세(Rddbc120)"
    else:
        display_source = monthly_source
        stock_source = monthly_stock_source

    return {
        "display_source": display_source,
        "stock_source": stock_source,
        "monthly_source": monthly_source,
        "monthly_stock_source": monthly_stock_source,
        **policy,
    }


STOCK_SHORTAGE_INTERNAL_COLUMNS = {
    "당월입고수량",
    "당월출고수량",
    "당월재고증감수량",
    "제품코드_RAW",
    "입고총수량",
    "출고총수량",
    "현재재고수량원본",
    "입고공급가액합계",
    "거래처코드",
    "거래처명",
    "시도명",
    "시구군명",
    "법정읍면동명",
    "출고건수",
}


def _finalize_stock_shortage_public_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

    attrs = dict(getattr(df, "attrs", {}) or {})
    drop_cols = [
        c for c in df.columns
        if str(c or "").endswith(("_x", "_y"))
        or str(c or "").strip() in STOCK_SHORTAGE_INTERNAL_COLUMNS
    ]
    out = df.drop(columns=drop_cols, errors="ignore").copy()
    out.attrs.update(attrs)
    return out

def _chunks(values: list[str], size: int):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _load_product_current_month_stock_movements(
    product_codes: list[str],
    *,
    stock_mode: str,
    date_to: str,
    stock_cd_list: Any = None,
    stock_cd: Any = None,
) -> pd.DataFrame:
    codes = sorted({str(x or "").strip() for x in product_codes if str(x or "").strip()})
    columns = ["제품코드", "당월입고수량", "당월출고수량", "당월재고증감수량"]
    if not codes:
        return pd.DataFrame(columns=columns)

    date_to = _normalize_date_to(date_to)
    if not date_to:
        return pd.DataFrame(columns=columns)
    date_from = f"{date_to[:6]}01"

    real_mode = str(stock_mode or "").strip() == "real"
    in_date_field = "T.Rd11_In_YyMmDd" if real_mode else "T.Rd11_Trans_YyMmDd"
    out_date_field = "T.Rd12_Out_YyMmDd" if real_mode else "T.Rd12_Trans_YyMmDd"
    in_qty_expr = (
        "CAST(ISNULL(T.Rd11_Quantity, 0) AS FLOAT) + CAST(ISNULL(T.Rd11_Oquantity, 0) AS FLOAT)"
        if real_mode
        else "CAST(ISNULL(T.Rd11_Quantity, 0) AS FLOAT)"
    )
    out_qty_expr = (
        "CAST(ISNULL(T.Rd12_Quantity, 0) AS FLOAT) + CAST(ISNULL(T.Rd12_Oquantity, 0) AS FLOAT)"
        if real_mode
        else "CAST(ISNULL(T.Rd12_Quantity, 0) AS FLOAT)"
    )
    in_exclude = ("2",) if real_mode else ("3",)
    out_exclude = ("7",) if real_mode else ("8",)

    stock_codes = _clean_list_param(stock_cd_list)
    if not stock_codes and clean_text(stock_cd):
        stock_codes = [clean_text(stock_cd)]

    def _stock_filter(field: str, bind_params: Dict[str, Any], prefix: str) -> str:
        if not stock_codes:
            return ""
        names: list[str] = []
        for i, cd in enumerate(stock_codes):
            key = f"{prefix}_stock_cd_{i}"
            bind_params[key] = clean_text(cd)
            names.append(f"%({key})s")
        return f"\n      AND {field} IN ({', '.join(names)})"

    def _query_batch(batch_codes: list[str]) -> pd.DataFrame:
        bind_params: Dict[str, Any] = {
            "date_from": date_from,
            "date_to": date_to,
        }
        placeholders: list[str] = []
        for i, cd in enumerate(batch_codes):
            key = f"cd{i}"
            bind_params[key] = cd
            placeholders.append(f"%({key})s")

        in_stock_filter = _stock_filter("T.Rd11_Stock_Cd", bind_params, "in")
        out_stock_filter = _stock_filter("T.Rd12_Stock_Cd", bind_params, "out")
        in_exclude_sql = ", ".join(f"'{x}'" for x in in_exclude)
        out_exclude_sql = ", ".join(f"'{x}'" for x in out_exclude)

        sql = f"""
WITH InAgg AS (
    SELECT
        LTRIM(RTRIM(T.Rd11_Physic_Cd)) AS [제품코드],
        SUM({in_qty_expr}) AS [당월입고수량]
    FROM dbo.Rddbc110 AS T WITH (NOLOCK)
    WHERE T.Rd11_Physic_Cd IN ({",".join(placeholders)})
      AND NULLIF(LTRIM(RTRIM({in_date_field})), '') IS NOT NULL
      AND {in_date_field} >= %(date_from)s
      AND {in_date_field} <= %(date_to)s
      AND LEFT(LTRIM(RTRIM(T.Rd11_Io_Gu)), 1) NOT IN ({in_exclude_sql})
      {in_stock_filter}
    GROUP BY LTRIM(RTRIM(T.Rd11_Physic_Cd))
),
OutAgg AS (
    SELECT
        LTRIM(RTRIM(T.Rd12_Physic_Cd)) AS [제품코드],
        SUM({out_qty_expr}) AS [당월출고수량]
    FROM dbo.Rddbc120 AS T WITH (NOLOCK)
    WHERE T.Rd12_Physic_Cd IN ({",".join(placeholders)})
      AND NULLIF(LTRIM(RTRIM({out_date_field})), '') IS NOT NULL
      AND {out_date_field} >= %(date_from)s
      AND {out_date_field} <= %(date_to)s
      AND LEFT(LTRIM(RTRIM(T.Rd12_Io_Gu)), 1) NOT IN ({out_exclude_sql})
      {out_stock_filter}
    GROUP BY LTRIM(RTRIM(T.Rd12_Physic_Cd))
)
SELECT
    COALESCE(I.[제품코드], O.[제품코드]) AS [제품코드],
    CAST(ISNULL(I.[당월입고수량], 0) AS FLOAT) AS [당월입고수량],
    CAST(ISNULL(O.[당월출고수량], 0) AS FLOAT) AS [당월출고수량],
    CAST(ISNULL(I.[당월입고수량], 0) - ISNULL(O.[당월출고수량], 0) AS FLOAT) AS [당월재고증감수량]
FROM InAgg AS I
FULL OUTER JOIN OutAgg AS O
    ON I.[제품코드] = O.[제품코드]
"""
        df = query_to_df(sql, bind_params)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame(columns=columns)

    frames: list[pd.DataFrame] = []
    for batch in _chunks(codes, 1800):
        df = _query_batch(batch)
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=columns)
    out = pd.concat(frames, ignore_index=True)
    for c in columns:
        if c not in out.columns:
            out[c] = 0 if c != "제품코드" else ""
    out["제품코드"] = out["제품코드"].fillna("").astype(str).str.strip()
    for c in ["당월입고수량", "당월출고수량", "당월재고증감수량"]:
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)
    return out.groupby("제품코드", as_index=False)[["당월입고수량", "당월출고수량", "당월재고증감수량"]].sum()


def _load_product_current_stock(
    product_codes: list[str],
    *,
    stock_mode: str,
    month_to: str,
    date_to: Any = None,
    policy_date: Any = None,
    stock_cd_list: Any = None,
    stock_cd: Any = None,
) -> pd.DataFrame:
    """
    품목별 재고부족현황의 현재고를 월집계 누계로 가져온다.

    SQL Server parameter limit을 피하기 위해 product code를 batch로 나누되,
    전체 product code를 조회한다.
    """
    t0 = time.perf_counter()
    codes = [str(x or "").strip() for x in product_codes if str(x or "").strip()]
    codes = sorted(set(codes))

    spec = _stock_current_monthly_spec(stock_mode)
    table = spec["table"]
    pfx = spec["prefix"]
    qty_col = spec["qty_col"]
    amt_col = spec["amt_col"]
    unit_col = spec["unit_col"]
    fallback_unit_col = spec["fallback_unit_col"]
    source_policy = _resolve_period_source_policy({"date_to": date_to, "policy_date": policy_date})
    stock_month_to = _normalize_month(month_to) or source_policy["effective_month_to"]
    detail_date_to = str(source_policy.get("effective_date_to") or "")
    use_mid_month_detail = bool(source_policy.get("use_hybrid"))
    monthly_stock_month_to = _prev_yyyymm(detail_date_to[:6]) if use_mid_month_detail else stock_month_to
    if not monthly_stock_month_to:
        monthly_stock_month_to = stock_month_to

    def _with_stock_attrs(df: pd.DataFrame, *, batches: int, elapsed: float) -> pd.DataFrame:
        df.attrs["stock_source_table"] = spec["source_table"]
        df.attrs["stock_source_label"] = (
            f"전월말 {spec['source_label']} + 당월 입출고상세"
            if use_mid_month_detail
            else spec["source_label"]
        )
        df.attrs["stock_cutoff_month"] = detail_date_to if use_mid_month_detail else stock_month_to
        df.attrs["stock_query_codes"] = len(codes)
        df.attrs["stock_query_batches"] = batches
        df.attrs["stock_query_elapsed_sec"] = elapsed
        return df

    if not codes:
        return _with_stock_attrs(pd.DataFrame(), batches=0, elapsed=0.0)

    if str(stock_mode or "").strip() == "real":
        in_qty_expr = (
            f"CAST(ISNULL(M.{pfx}_In_Quantity, 0) AS FLOAT)"
            f" + CAST(ISNULL(M.{pfx}_In_Oquantity, 0) AS FLOAT)"
        )
        out_qty_expr = (
            f"CAST(ISNULL(M.{pfx}_Out_Quantity, 0) AS FLOAT)"
            f" + CAST(ISNULL(M.{pfx}_Out_Oquantity, 0) AS FLOAT)"
        )
    else:
        in_qty_expr = f"CAST(ISNULL(M.{pfx}_In_Quantity, 0) AS FLOAT)"
        out_qty_expr = f"CAST(ISNULL(M.{pfx}_Out_Quantity, 0) AS FLOAT)"

    stock_codes = _clean_list_param(stock_cd_list)
    if not stock_codes and clean_text(stock_cd):
        stock_codes = [clean_text(stock_cd)]
    stock_codes = [clean_text(x) for x in stock_codes if clean_text(x)]

    def _query_batch(batch_codes: list[str]) -> pd.DataFrame:
        bind_params: Dict[str, Any] = {
            "stock_month_to": monthly_stock_month_to,
        }
        placeholders: list[str] = []

        for i, cd in enumerate(batch_codes):
            key = f"cd{i}"
            bind_params[key] = cd
            placeholders.append(f"%({key})s")

        stock_filter_sql = ""
        if stock_codes:
            stock_names: list[str] = []
            for i, cd in enumerate(stock_codes):
                key = f"stock_cd_{i}"
                bind_params[key] = cd
                stock_names.append(f"%({key})s")
            stock_filter_sql = f"\n      AND M.{pfx}_Stock_Cd IN ({', '.join(stock_names)})"

        sql = f"""
WITH StockAgg AS (
    SELECT
        M.{pfx}_Physic_Cd AS [제품코드_RAW],

        SUM(
            {in_qty_expr}
        ) AS [입고총수량],

        SUM(
            {out_qty_expr}
        ) AS [출고총수량],

        SUM(
            {in_qty_expr}
          - {out_qty_expr}
        ) AS [현재재고수량원본],

        SUM(
            CAST(ISNULL(M.{pfx}_In_Supply_Price, 0) AS FLOAT)
        ) AS [입고공급가액합계]

    FROM {table} AS M WITH (NOLOCK)

    WHERE M.{pfx}_Physic_Cd IN ({",".join(placeholders)})
      AND M.{pfx}_Stock_YyMm <= %(stock_month_to)s
      {stock_filter_sql}

    GROUP BY
        M.{pfx}_Physic_Cd
)
SELECT
    LTRIM(RTRIM(S.[제품코드_RAW])) AS [제품코드],

    CAST(ISNULL(S.[현재재고수량원본], 0) AS FLOAT) AS [{qty_col}],

    CAST(
        CASE
            WHEN ABS(ISNULL(S.[입고총수량], 0)) > 0
                THEN ISNULL(S.[입고공급가액합계], 0) / NULLIF(S.[입고총수량], 0)
            ELSE ISNULL(P.{fallback_unit_col}, 0)
        END
        AS FLOAT
    ) AS [{unit_col}],

    CAST(
        ISNULL(S.[현재재고수량원본], 0)
        *
        CASE
            WHEN ABS(ISNULL(S.[입고총수량], 0)) > 0
                THEN ISNULL(S.[입고공급가액합계], 0) / NULLIF(S.[입고총수량], 0)
            ELSE ISNULL(P.{fallback_unit_col}, 0)
        END
        AS FLOAT
    ) AS [{amt_col}]

FROM StockAgg AS S

LEFT JOIN dbo.Rddbc040 AS P WITH (NOLOCK)
    ON S.[제품코드_RAW] = P.Rd04_Physic_Cd
OPTION (RECOMPILE)
"""

        batch_df = query_to_df(sql, bind_params)
        if batch_df is None:
            return pd.DataFrame()
        return batch_df

    chunk_size = 1800
    batches = list(_chunks(codes, chunk_size))
    frames: list[pd.DataFrame] = []

    for batch in batches:
        batch_df = _query_batch(batch)
        if batch_df is not None and not batch_df.empty:
            frames.append(batch_df)

    if frames:
        stock_df = pd.concat(frames, ignore_index=True)
    else:
        stock_df = pd.DataFrame()

    if use_mid_month_detail:
        movement_df = _load_product_current_month_stock_movements(
            codes,
            stock_mode=stock_mode,
            date_to=detail_date_to,
            stock_cd_list=stock_cd_list,
            stock_cd=stock_cd,
        )
        if movement_df is not None and not movement_df.empty:
            if stock_df is None or stock_df.empty:
                stock_df = movement_df[["제품코드"]].copy()
                stock_df[qty_col] = 0
                stock_df[unit_col] = 0
                stock_df[amt_col] = 0
            stock_df["제품코드"] = stock_df["제품코드"].fillna("").astype(str).str.strip()
            stock_df = stock_df.merge(movement_df, on="제품코드", how="outer")
            for c in [qty_col, unit_col, amt_col, "당월입고수량", "당월출고수량", "당월재고증감수량"]:
                if c not in stock_df.columns:
                    stock_df[c] = 0
                stock_df[c] = pd.to_numeric(stock_df[c], errors="coerce").fillna(0)
            stock_df[qty_col] = stock_df[qty_col] + stock_df["당월재고증감수량"]
            stock_df[amt_col] = stock_df[qty_col] * stock_df[unit_col]

    product_col = "제품코드"
    if not stock_df.empty and product_col in stock_df.columns:
        dup_count = int(stock_df[product_col].fillna("").astype(str).str.strip().duplicated().sum())
        if dup_count:
            log.warning(
                "[analytics.stock.load.duplicate_product_code] duplicates=%s codes=%s batches=%s",
                dup_count,
                len(codes),
                len(batches),
            )

    elapsed = time.perf_counter() - t0
    stock_df = _with_stock_attrs(stock_df, batches=len(batches), elapsed=elapsed)
    log.info(
        "[analytics.stock.load.perf] codes=%s batches=%s rows=%s elapsed=%.3fs stock_mode=%s stock_cutoff_month=%s stock_cd_count=%s",
        len(codes),
        len(batches),
        len(stock_df),
        elapsed,
        stock_mode,
        detail_date_to if use_mid_month_detail else stock_month_to,
        len(stock_codes),
    )

    return stock_df

def _month_qty_columns(df: pd.DataFrame) -> list[str]:
    if df is None or df.empty:
        return []

    cols = []
    for c in df.columns:
        s = str(c or "")
        if s.endswith(" 수량") and len(s) >= 8 and s[:4].isdigit():
            cols.append(c)

    return sorted(cols)


def _build_qty_workforward_metrics_from_wide(
    df: pd.DataFrame,
    qty_month_pairs: list[tuple[str, str]],
    params: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    if df is None or df.empty or "제품코드" not in df.columns or not qty_month_pairs:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    for month, col in qty_month_pairs:
        if col not in df.columns:
            continue
        part = df[["제품코드", col]].copy()
        part["기준월"] = month
        part["수요수량"] = pd.to_numeric(part[col], errors="coerce").fillna(0)
        frames.append(part[["제품코드", "기준월", "수요수량"]])

    if not frames:
        return pd.DataFrame()

    monthly = (
        pd.concat(frames, ignore_index=True)
        .groupby(["제품코드", "기준월"], dropna=False, as_index=False)["수요수량"]
        .sum()
        .sort_values(["제품코드", "기준월"])
        .reset_index(drop=True)
    )
    months = sorted({_parse_yyyymm(v) for v in monthly["기준월"].dropna().tolist()})
    _completed_months, evaluation_month, _future_months = _split_sales_period_months(months, params)

    grp = monthly.groupby("제품코드", dropna=False)
    monthly["_이전월수요"] = grp["수요수량"].shift(1)
    monthly["평가월 이전 완료월수"] = grp.cumcount()
    monthly["완료월총수요수량"] = grp["수요수량"].cumsum().shift(1)
    monthly["완료월총수요수량"] = monthly["완료월총수요수량"].where(monthly["평가월 이전 완료월수"] > 0, 0).fillna(0)
    monthly["완료월평균수요수량"] = (
        monthly["완료월총수요수량"] / monthly["평가월 이전 완료월수"].replace(0, 1)
    ).where(monthly["평가월 이전 완료월수"] > 0, 0)
    monthly["최근3개월평균수요수량"] = (
        monthly.groupby("제품코드", dropna=False)["_이전월수요"]
        .rolling(3, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
        .fillna(0)
    )
    monthly["최근6개월평균수요수량"] = (
        monthly.groupby("제품코드", dropna=False)["_이전월수요"]
        .rolling(6, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
        .fillna(0)
    )
    monthly["수요증감률"] = [
        _pct_change(r3, r6)
        for r3, r6 in zip(monthly["최근3개월평균수요수량"].tolist(), monthly["최근6개월평균수요수량"].tolist())
    ]
    projection_src = pd.DataFrame({
        "완료월총매출": monthly["완료월총수요수량"],
        "완료월수": monthly["평가월 이전 완료월수"],
        "완료월평균매출": monthly["완료월평균수요수량"],
        "월평균매출": monthly["완료월평균수요수량"],
        "최근3개월평균매출": monthly["최근3개월평균수요수량"],
        "최근6개월평균매출": monthly["최근6개월평균수요수량"],
        "최근3개월증감률": monthly["수요증감률"],
        "매출발생월수": monthly["평가월 이전 완료월수"],
        "추세판정": "",
    })
    projection = projection_src.apply(lambda r: _forecast_projection_from_row(r), axis=1, result_type="expand")
    if not projection.empty:
        projection.columns = ["수요예상기준", "수요적용증감률", "평가월 예상수요수량"]
        monthly["수요예상기준"] = projection["수요예상기준"].replace({
            "최근3개월평균매출": "최근3개월평균수요수량",
            "최근6개월평균매출": "최근6개월평균수요수량",
            "완료월평균매출": "완료월평균수요수량",
            "월평균매출": "완료월평균수요수량",
            "자료부족": "비교자료부족",
        })
        monthly["수요적용증감률"] = projection["수요적용증감률"]
        monthly["평가월 예상수요수량"] = projection["평가월 예상수요수량"]
    else:
        monthly["수요예상기준"] = "비교자료부족"
        monthly["수요적용증감률"] = 0
        monthly["평가월 예상수요수량"] = 0
    monthly["예상기준월수량"] = projection_src.apply(lambda r: _forecast_base_and_label(r)[0], axis=1)
    monthly.loc[monthly["평가월 이전 완료월수"] <= 0, "평가월 예상수요수량"] = 0
    monthly.loc[monthly["평가월 이전 완료월수"] <= 0, "예상기준월수량"] = 0
    monthly["평가월 실제수요수량"] = monthly["수요수량"]
    monthly["평가월 잔여예상수요수량"] = (monthly["평가월 예상수요수량"] - monthly["평가월 실제수요수량"]).clip(lower=0)
    monthly["평가월 수요진척률"] = [
        (actual / expected * 100) if abs(float(expected or 0)) >= 1e-12 else 0
        for actual, expected in zip(monthly["평가월 실제수요수량"].tolist(), monthly["평가월 예상수요수량"].tolist())
    ]

    if evaluation_month:
        monthly = monthly[monthly["기준월"].astype(str) == evaluation_month].copy()
    return monthly.drop(columns=[c for c in ["_이전월수요"] if c in monthly.columns])


def get_stock_shortage_df(
    params: Optional[Dict[str, Any]] = None,
    sales_raw_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    품목별 재고부족현황 1차.

    기준:
    - 판매/출고 흐름: get_sales_forecast_df() 재사용
    - 현재재고: Rddbc040 현재 장부/실재고
    - 부족판정: 최근 수량 평균 대비 현재재고 커버월수
    """
    params = coalesce_params(params)
    params = _apply_month_or_date_params(params)
    params = _apply_period_source_policy_params(params)
    t0 = time.perf_counter()

    stock_mode = str(params.get("stock_mode") or "book").strip()
    stock_label = _stock_mode_label(stock_mode)
    source_labels = _stock_shortage_source_labels(params, stock_mode=stock_mode)

    base = get_sales_forecast_df(params, raw_df=sales_raw_df)
    t_base = time.perf_counter()
    if base is None or base.empty:
        log.info(
            "[analytics.stock_shortage.perf] base_empty elapsed=%.3fs params=%r",
            t_base - t0,
            params,
        )
        return pd.DataFrame()

    out = base.copy()

    if "제품코드" not in out.columns:
        return pd.DataFrame()

    product_codes = out["제품코드"].fillna("").astype(str).str.strip().tolist()

    stock_cutoff_month = _stock_current_cutoff_month(params)
    stock_spec = _stock_current_monthly_spec(stock_mode)

    stock_df = _load_product_current_stock(
        product_codes,
        stock_mode=stock_mode,
        month_to=stock_cutoff_month,
        date_to=params.get("date_to"),
        policy_date=params.get("policy_date") or params.get("as_of_date") or params.get("today"),
        stock_cd_list=params.get("stock_cd_list"),
        stock_cd=params.get("stock_cd"),
    )
    t_stock = time.perf_counter()

    if stock_df is None or stock_df.empty:
        out["장부재고수량"] = 0
        out["실재고수량"] = 0
        out["장부재고금액"] = 0
        out["실재고금액"] = 0
    else:
        stock_df = stock_df.copy()

        for c in [
            "장부재고수량",
            "실재고수량",
            "장부재고금액",
            "실재고금액",
            "장부재고평가단가",
            "실재고평가단가",
        ]:
            if c in stock_df.columns:
                stock_df[c] = pd.to_numeric(stock_df[c], errors="coerce").fillna(0)

        out = out.merge(stock_df, on="제품코드", how="left")


    for c in ["장부재고수량", "실재고수량", "장부재고금액", "실재고금액"]:
        if c not in out.columns:
            out[c] = 0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)

    qty_cols = _month_qty_columns(out)
    qty_month_pairs: list[tuple[str, str]] = []
    for c in qty_cols:
        label = str(c or "").strip()
        month = _parse_yyyymm(label[:7])
        if month:
            qty_month_pairs.append((month, c))
    qty_month_pairs = sorted(qty_month_pairs, key=lambda x: x[0])
    months = [m for m, _c in qty_month_pairs]
    qty_workforward = _build_qty_workforward_metrics_from_wide(out, qty_month_pairs, params)
    if qty_workforward is not None and not qty_workforward.empty:
        qty_cols_for_merge = [
            "제품코드",
            "평가월 이전 완료월수",
            "완료월총수요수량",
            "완료월평균수요수량",
            "최근3개월평균수요수량",
            "최근6개월평균수요수량",
            "수요증감률",
            "수요적용증감률",
            "수요예상기준",
            "예상기준월수량",
            "평가월 예상수요수량",
            "평가월 실제수요수량",
            "평가월 잔여예상수요수량",
            "평가월 수요진척률",
        ]
        out = out.merge(
            qty_workforward[[c for c in qty_cols_for_merge if c in qty_workforward.columns]].drop_duplicates("제품코드"),
            on="제품코드",
            how="left",
            validate="one_to_one",
        )
    for c in [
        "평가월 이전 완료월수",
        "완료월총수요수량",
        "완료월평균수요수량",
        "최근3개월평균수요수량",
        "최근6개월평균수요수량",
        "수요증감률",
        "수요적용증감률",
        "예상기준월수량",
        "평가월 예상수요수량",
        "평가월 실제수요수량",
        "평가월 잔여예상수요수량",
        "평가월 수요진척률",
    ]:
        if c not in out.columns:
            out[c] = 0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)
    if "수요예상기준" not in out.columns:
        out["수요예상기준"] = "비교자료부족"
    out["완료월수"] = out["평가월 이전 완료월수"]
    out["완료월총출고수량"] = out["완료월총수요수량"]
    out["완료월평균출고수량"] = out["완료월평균수요수량"]
    out["월평균출고수량"] = out["완료월평균출고수량"]
    out["최근3개월평균수량"] = out["최근3개월평균수요수량"]
    out["최근6개월평균수량"] = out["최근6개월평균수요수량"]
    out["최근3개월평균출고수량"] = out["최근3개월평균수요수량"]
    out["최근6개월평균출고수량"] = out["최근6개월평균수요수량"]
    out["최근3개월수량증감률"] = out["수요증감률"]
    out["당월 현재출고수량"] = out["평가월 실제수요수량"]
    out["당월 예상출고수량"] = out["평가월 예상수요수량"]
    out["당월 잔여예상출고수량"] = out["평가월 잔여예상수요수량"]
    out["당월 출고진척률"] = out["평가월 수요진척률"]

    stock_qty_col = _stock_qty_col(stock_mode)
    stock_amt_col = _stock_amt_col(stock_mode)
    stock_unit_col = "실재고평가단가" if stock_mode == "real" else "장부재고평가단가"

    out["재고기준"] = stock_label
    out["현재재고수량"] = pd.to_numeric(out[stock_qty_col], errors="coerce").fillna(0)
    out["현재재고금액"] = pd.to_numeric(out[stock_amt_col], errors="coerce").fillna(0)
    if stock_unit_col not in out.columns:
        out[stock_unit_col] = 0
    out["재고평가단가"] = pd.to_numeric(out[stock_unit_col], errors="coerce").fillna(0)

    out["1개월필요수량"] = out["예상기준월수량"]
    out["2개월필요수량"] = out["예상기준월수량"] * 2
    out["3개월필요수량"] = out["예상기준월수량"] * 3

    out["1개월부족수량"] = (out["1개월필요수량"] - out["현재재고수량"]).clip(lower=0)
    out["2개월부족수량"] = (out["2개월필요수량"] - out["현재재고수량"]).clip(lower=0)
    out["3개월부족수량"] = (out["3개월필요수량"] - out["현재재고수량"]).clip(lower=0)

    def _calc_stock_cover_months(row: pd.Series) -> float:
        stock_qty = _safe_float(row.get("현재재고수량"))
        avg_qty = _safe_float(row.get("예상기준월수량"))

        # 수요가 없고 재고가 있으면 부족 위험이 낮으므로 큰 값
        if avg_qty <= 0 and stock_qty > 0:
            return 999

        # 수요도 없고 재고도 없거나, 재고가 마이너스면 커버월수는 0으로 본다
        if avg_qty <= 0:
            return 0

        if stock_qty <= 0:
            return 0

        return stock_qty / avg_qty


    out["재고커버월수"] = out.apply(_calc_stock_cover_months, axis=1)

    out["부족등급"] = out.apply(_stock_shortage_grade, axis=1)

    remaining_out_qty = pd.to_numeric(out["당월 잔여예상출고수량"], errors="coerce").fillna(0)
    current_stock_qty = pd.to_numeric(out["현재재고수량"], errors="coerce").fillna(0)
    unit_price = pd.to_numeric(out["재고평가단가"], errors="coerce").fillna(0)
    out["예상월말재고수량"] = current_stock_qty - remaining_out_qty
    out["부족예상수량"] = (remaining_out_qty - current_stock_qty).clip(lower=0)
    out["부족예상금액"] = out["부족예상수량"] * unit_price.clip(lower=0)
    out["당월 재고충족률"] = [
        (max(float(stock), 0.0) / float(remain) * 100) if float(remain or 0) > 0 else 100.0
        for stock, remain in zip(current_stock_qty.tolist(), remaining_out_qty.tolist())
    ]
    out["재고부족판정"] = out.apply(_stock_shortage_current_judge, axis=1)

    shortage_grade_filter = clean_text(
        params.get("shortage_grade") or params.get("shortage_grade_filter")
    )
    if shortage_grade_filter and shortage_grade_filter != "전체":
        out = out[
            out["부족등급"].fillna("").astype(str).str.strip() == shortage_grade_filter
        ].copy()

    # 표시/다운로드용 소수점 정리
    # - 재고부족현황 원본표에서 평균/예상/커버/필요/부족 수량이
    #   12.333333333 처럼 길게 보이지 않도록 2자리로 정리한다.
    shortage_decimal_cols = [
        "최근3개월평균수량",
        "최근6개월평균수량",
        "최근3개월평균출고수량",
        "최근6개월평균출고수량",
        "월평균출고수량",
        "완료월총출고수량",
        "완료월평균출고수량",
        "완료월총수요수량",
        "완료월평균수요수량",
        "최근3개월평균수요수량",
        "최근6개월평균수요수량",
        "수요증감률",
        "수요적용증감률",
        "평가월 예상수요수량",
        "평가월 실제수요수량",
        "평가월 잔여예상수요수량",
        "평가월 수요진척률",
        "예상기준월수량",
        "당월 예상출고수량",
        "당월 잔여예상출고수량",
        "예상월말재고수량",
        "부족예상수량",
        "부족예상금액",
        "재고커버월수",
        "1개월필요수량",
        "2개월필요수량",
        "3개월필요수량",
        "1개월부족수량",
        "2개월부족수량",
        "3개월부족수량",
    ]

    for c in shortage_decimal_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0).round(2)

    front_cols = [c for c in [
        "제품코드",
        "제품명",
        "규격",
        "제조사명",
        "매입처명",
        "제품그룹명",
        "제품구분명",
        "제품분류명",
        "재고기준",

        "완료월평균출고수량",
        "최근3개월평균수요수량",
        "최근6개월평균수요수량",
        "수요증감률",
        "수요적용증감률",
        "평가월 예상수요수량",
        "평가월 실제수요수량",
        "평가월 잔여예상수요수량",
        "당월 현재출고수량",
        "당월 예상출고수량",
        "당월 잔여예상출고수량",
        "당월 출고진척률",
        "현재재고수량",
        "재고평가단가",
        "예상월말재고수량",
        "부족예상수량",
        "부족예상금액",
        "당월 재고충족률",
        "재고부족판정",
        "현재재고금액",
        "장부재고평가단가",
        "실재고평가단가",
        "완료월수",
        "완료월총출고수량",
        "최근3개월평균출고수량",
        "최근6개월평균출고수량",
        "최근3개월수량증감률",
        "최근3개월평균수량",

        "최근6개월평균수량",
        "월평균출고수량",
        "예상기준월수량",
        "재고커버월수",
        "1개월필요수량",
        "1개월부족수량",
        "2개월필요수량",
        "2개월부족수량",
        "3개월필요수량",
        "3개월부족수량",
        "부족등급",
        "총출고수량",
        "총매출액",
        "월평균매출",
        "최근3개월평균매출",
        "최근6개월평균매출",
        "추세판정",
        "예상등급",
        "매입처수",
        "거래처수",
        "재고적용처수",
    ] if c in out.columns]

    monthly_cols = [
        c for c in out.columns
        if isinstance(c, str) and (c.endswith(" 매출") or c.endswith(" 수량")) and c not in front_cols
    ]

    rest_cols = [c for c in out.columns if c not in front_cols + monthly_cols]
    out = out[front_cols + monthly_cols + rest_cols]

    out = out.sort_values(
        ["부족예상금액", "부족예상수량", "당월 재고충족률", "제품코드"],
        ascending=[False, False, True, True],
    ).reset_index(drop=True)

    if "순번" in out.columns:
        out = out.drop(columns=["순번"])

    out.insert(0, "순번", range(1, len(out) + 1))

    out["분석자료원"] = source_labels["display_source"]
    out["현재고원천"] = source_labels["stock_source"]

    out = _finalize_stock_shortage_public_df(out)

    out.attrs["stock_source_table"] = stock_spec.get("source_table")
    out.attrs["stock_source_label"] = source_labels["stock_source"]
    out.attrs["display_source_label"] = source_labels["display_source"]
    out.attrs["evaluation_mode"] = source_labels.get("evaluation_mode")
    out.attrs["use_hybrid_detail"] = bool(source_labels.get("use_hybrid_detail"))
    out.attrs["stock_cutoff_month"] = stock_cutoff_month
    out.attrs["stock_mode"] = stock_mode

    log.info(
        "[analytics.stock_shortage.source] evaluation_mode=%s use_hybrid_detail=%s display_source=%s stock_source=%s",
        source_labels.get("evaluation_mode"),
        bool(source_labels.get("use_hybrid_detail")),
        source_labels["display_source"],
        source_labels["stock_source"],
    )

    t_done = time.perf_counter()
    log.info(
        "[analytics.stock_shortage.perf] base_rows=%s stock_rows=%s out_rows=%s base=%.3fs stock=%.3fs build=%.3fs total=%.3fs stock_mode=%s stock_cutoff_month=%s",
        len(base),
        0 if stock_df is None else len(stock_df),
        len(out),
        t_base - t0,
        t_stock - t_base,
        t_done - t_stock,
        t_done - t0,
        stock_mode,
        stock_cutoff_month,
    )

    return _normalize_analytics_numeric_columns(out)


def _stock_shortage_meta_from_df(df: pd.DataFrame) -> Dict[str, Any]:
    if df is None or df.empty:
        return {
            "row_count": 0,
            "row_count_total": 0,
            "product_count": 0,
            "sum_current_stock_qty": 0,
            "sum_current_stock_amt": 0,
            "sum_completed_month_avg_out_qty": 0,
            "sum_current_month_out_qty": 0,
            "sum_current_month_expected_out_qty": 0,
            "sum_current_month_remaining_out_qty": 0,
            "current_month_demand_progress_pct": 0,
            "sum_expected_shortage_qty": 0,
            "sum_expected_shortage_amt": 0,
            "overall_stock_fill_rate": 100,
            "sum_shortage_1m_qty": 0,
            "sum_shortage_2m_qty": 0,
            "sum_shortage_3m_qty": 0,
            "shortage_item_count": 0,
            "shortage_grade_counts": {},
            "stock_shortage_judge_counts": {},
        }

    out = df.copy()

    if "재고부족판정" in out.columns:
        shortage_item_count = int((out["재고부족판정"].fillna("").astype(str) == "부족").sum())
    elif "부족등급" in out.columns:
        shortage_item_count = int(
            out["부족등급"]
            .fillna("")
            .astype(str)
            .isin(["재고없음", "1개월내 부족", "2개월내 부족주의", "3개월내 부족주의", "3개월내 부족"])
            .sum()
        )
    else:
        shortage_item_count = 0

    expected_total = _sum_numeric(out, "당월 예상출고수량")
    actual_total = _sum_numeric(out, "당월 현재출고수량")
    demand_progress_pct = (actual_total / expected_total * 100) if expected_total > 0 else 0.0

    remaining_total = _sum_numeric(out, "당월 잔여예상출고수량")
    current_stock_positive_total = 0.0
    if "현재재고수량" in out.columns:
        current_stock_positive_total = float(pd.to_numeric(out["현재재고수량"], errors="coerce").fillna(0).clip(lower=0).sum())
    overall_fill_rate = (
        current_stock_positive_total / remaining_total * 100
        if remaining_total > 0
        else 100.0
    )

    shortage_by_current_policy = _sum_numeric(out, "부족예상수량")
    if shortage_by_current_policy > 0 and "부족예상수량" in out.columns:
        shortage_item_count = int((pd.to_numeric(out["부족예상수량"], errors="coerce").fillna(0) > 0).sum())

    return {
        "row_count": int(len(out)),
        "row_count_total": int(len(out)),
        "product_count": int(out["제품코드"].nunique()) if "제품코드" in out.columns else int(len(out)),
        
        "sum_current_stock_qty": _sum_numeric(out, "현재재고수량"),
        "sum_current_stock_amt": _sum_numeric(out, "현재재고금액"),
        "sum_completed_month_avg_out_qty": _sum_numeric(out, "완료월평균출고수량"),
        "sum_current_month_out_qty": _sum_numeric(out, "당월 현재출고수량"),
        "sum_current_month_expected_out_qty": _sum_numeric(out, "당월 예상출고수량"),
        "sum_current_month_remaining_out_qty": _sum_numeric(out, "당월 잔여예상출고수량"),
        "sum_eval_actual_demand_qty": actual_total,
        "sum_eval_expected_demand_qty": expected_total,
        "sum_eval_remaining_demand_qty": remaining_total,
        "current_month_demand_progress_pct": demand_progress_pct,
        "eval_demand_progress_pct": demand_progress_pct,
        "sum_expected_shortage_qty": _sum_numeric(out, "부족예상수량"),
        "sum_expected_shortage_amt": _sum_numeric(out, "부족예상금액"),
        "overall_stock_fill_rate": overall_fill_rate,
        "sum_shortage_1m_qty": _sum_numeric(out, "1개월부족수량"),
        "sum_shortage_2m_qty": _sum_numeric(out, "2개월부족수량"),
        "sum_shortage_3m_qty": _sum_numeric(out, "3개월부족수량"),

        "shortage_item_count": shortage_item_count,
        "shortage_grade_counts": _shortage_grade_counts(out),
        "stock_shortage_judge_counts": _count_text_values(out, "재고부족판정"),
    }


def get_stock_shortage_result(
    params: Optional[Dict[str, Any]] = None,
    sales_raw_df: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    params = coalesce_params(params)
    params = _apply_month_or_date_params(params)

    df = get_stock_shortage_df(params, sales_raw_df=sales_raw_df)
    row_count = 0 if df is None else int(len(df))

    log.info("[analytics.stock_shortage] rows=%s params=%r", row_count, params)

    stock_mode = str(params.get("stock_mode") or "book").strip()
    stock_label = _stock_mode_label(stock_mode)
    stock_source_labels = _stock_shortage_source_labels(params, stock_mode=stock_mode)
    source_mode = _resolve_source_mode(params)
    source_label = str(getattr(df, "attrs", {}).get("display_source_label") or stock_source_labels["display_source"])
    query_summary = _fmt_analytics_query_summary(params, source_label)
    if stock_label:
        query_summary = (f"{query_summary} / 재고기준 {stock_label}" if query_summary else f"재고기준 {stock_label}")
    shortage_grade_filter = clean_text(
        params.get("shortage_grade") or params.get("shortage_grade_filter")
    )
    if shortage_grade_filter and shortage_grade_filter != "전체":
        query_summary = (
            f"{query_summary} / 부족등급 {shortage_grade_filter}"
            if query_summary
            else f"부족등급 {shortage_grade_filter}"
        )

    if row_count == 0:
        return {
            "table": TABLE,
            "title": "품목별 재고부족현황",
            "action": "품목별 재고부족현황",
            "params": params,
            "data": "해당 조회조건의 자료가 없습니다.",
            "message": "해당 조회조건의 자료가 없습니다.",
            "final": True,
            "type": "text",
            "meta": {
                "row_count": 0,
                "row_count_total": 0,
                "analytics": True,
                "analysis_type": "stock_shortage",
                "summary_type": "product_stock_shortage",
                "source_mode": source_mode,
                "source_label": source_label,
                "stock_mode": stock_mode,
                "stock_label": stock_label,
                "stock_source_label": stock_source_labels["stock_source"],
                "display_source_label": stock_source_labels["display_source"],
                "evaluation_mode": stock_source_labels.get("evaluation_mode"),
                "use_hybrid_detail": bool(stock_source_labels.get("use_hybrid_detail")),
                "shortage_grade_counts": {},
                "stock_shortage_judge_counts": {},
                "query_summary": query_summary,
                "condition": query_summary,
                "summary_md": (
                    f"조회조건: {query_summary}\n\n"
                    "재고부족요약: 해당 조회조건의 자료가 없습니다."
                ),
            },
        }

    payload = build_result_payload(
        table=TABLE,
        title="품목별 재고부족현황",
        action="품목별 재고부족현황",
        params=params,
        df=df,
        message=f"품목별 재고부족현황 {row_count:,}건",
    )

    grade_counts = _shortage_grade_counts(df)

    meta = dict(payload.get("meta") or {})
    meta.update(_stock_shortage_meta_from_df(df))

    stock_source_table = df.attrs.get("stock_source_table", "")
    stock_source_label = df.attrs.get("stock_source_label", "") or stock_source_labels["stock_source"]
    display_source_label = df.attrs.get("display_source_label", "") or stock_source_labels["display_source"]
    stock_cutoff_month = df.attrs.get("stock_cutoff_month", "")

    meta.update({
        "row_count": int(row_count),
        "row_count_total": int(row_count),
        "analytics": True,
        "analysis_type": "stock_shortage",
        "summary_type": "product_stock_shortage",
        "source_mode": source_mode,
        "source_label": display_source_label,
        "stock_mode": stock_mode,
        "stock_label": stock_label,
        "shortage_grade_counts": grade_counts,

        "stock_source_table": stock_source_table,
        "stock_source_label": stock_source_label,
        "display_source_label": display_source_label,
        "evaluation_mode": df.attrs.get("evaluation_mode") or stock_source_labels.get("evaluation_mode"),
        "use_hybrid_detail": bool(df.attrs.get("use_hybrid_detail") or stock_source_labels.get("use_hybrid_detail")),
        "stock_cutoff_month": stock_cutoff_month,
        "query_summary": query_summary,
        "condition": query_summary,

        "summary_md": (
            f"조회조건 {query_summary} / "
            f"재고부족요약: "
            f"품목수 {_fmt_num_for_summary(row_count)} / "
            f"완료월평균출고수량 {_fmt_num_for_summary(meta.get('sum_completed_month_avg_out_qty'))} / "
            f"평가월실제수요 {_fmt_num_for_summary(meta.get('sum_current_month_out_qty'))} / "
            f"평가월예상수요 {_fmt_num_for_summary(meta.get('sum_current_month_expected_out_qty'))} / "
            f"평가월잔여예상수요 {_fmt_num_for_summary(meta.get('sum_current_month_remaining_out_qty'))} / "
            f"평가월수요진척률 {_fmt_num_for_summary(meta.get('current_month_demand_progress_pct'))}% / "
            f"현재재고수량 {_fmt_num_for_summary(meta.get('sum_current_stock_qty'))} / "
            f"부족예상수량 {_fmt_num_for_summary(meta.get('sum_expected_shortage_qty'))} / "
            f"부족예상금액 {_fmt_num_for_summary(meta.get('sum_expected_shortage_amt'))} / "
            f"전체재고충족률 {_fmt_num_for_summary(meta.get('overall_stock_fill_rate'))}% / "
            f"자료원 {display_source_label} / "
            f"재고기준 {stock_label} / "
            f"현재고원천 {stock_source_label or stock_label} / "
            f"현재고기준월 {stock_cutoff_month}"
        ),
    })

    payload["meta"] = meta
    return payload

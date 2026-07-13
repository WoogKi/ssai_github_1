# app/services/analytics_customer_sales_forecast_service.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import pandas as pd

from app.services.rddbc_io_common import build_result_payload, coalesce_params, like_value, query_to_df
from app.services.analytics_sales_trend_service import (
    TABLE,
    _apply_month_or_date_params,
    _apply_period_source_policy_params,
    _build_sales_month_workforward_metrics,
    _fmt_analytics_query_summary,
    _fmt_counts_for_summary,
    _fmt_num_for_summary,
    _fmt_yyyymm_col,
    _forecast_grade,
    _forecast_projection_from_row,
    _iter_yyyymm,
    _normalize_analytics_numeric_columns,
    _parse_yyyymm,
    _resolve_period_source_policy,
    _split_sales_period_months,
)

log = logging.getLogger("ssai.sims.analytics_customer_sales_forecast")

SOURCE_LABEL = "출고 거래명세서 원장매출(Rddbc130)"

CUSTOMER_FORECAST_COLUMNS = [
    "순번",
    "매출처코드",
    "매출처명",
    "영업사원코드",
    "담당영업사원명",
    "시도명",
    "시구군명",
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
    "분석자료원",
    "기간구분",
]


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any) -> float:
    try:
        if pd.isna(value):
            return 0.0
    except Exception:
        pass
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _display_period_label(policy: Optional[Dict[str, Any]]) -> str:
    mode = str((policy or {}).get("evaluation_mode") or "")
    return {
        "current_monthly": "현재월 월집계",
        "historical_midmonth": "과거 월중 상세",
        "historical_month_end": "과거 월말 월집계",
    }.get(mode, mode or "월집계")


def _eval_label_map(policy: Optional[Dict[str, Any]]) -> dict[str, str]:
    if str((policy or {}).get("evaluation_mode") or "") == "current_monthly":
        return {
            "당월 현재매출": "당월 현재매출",
            "당월 예상매출": "당월 예상매출",
            "당월 잔여예상": "당월 잔여예상",
            "당월 진척률": "당월 진척률",
        }
    return {
        "당월 현재매출": "평가월 매출",
        "당월 예상매출": "평가월 예상매출",
        "당월 잔여예상": "평가월 잔여예상",
        "당월 진척률": "평가월 진척률",
    }


def _progress_labels(policy: Optional[Dict[str, Any]]) -> dict[str, str]:
    if str((policy or {}).get("evaluation_mode") or "") == "current_monthly":
        return {
            "title": "당월 매출예상 요약",
            "sales": "당월 현재매출",
            "expected": "당월 예상매출",
            "remaining": "당월 잔여예상",
            "progress": "당월 진척률",
        }
    return {
        "title": "평가월 매출예상 요약",
        "sales": "평가월 매출",
        "expected": "평가월 예상매출",
        "remaining": "평가월 잔여예상",
        "progress": "평가월 진척률",
    }


def _month_range(params: Dict[str, Any], policy: Dict[str, Any]) -> list[str]:
    month_from = _parse_yyyymm(params.get("month_from"))
    month_to = _parse_yyyymm(policy.get("effective_month_to") or params.get("month_to"))
    if month_from and month_to:
        return _iter_yyyymm(month_from, month_to)
    return []


def _add_customer_filters(clauses: list[str], sql_params: Dict[str, Any], params: Dict[str, Any], alias: str = "V") -> None:
    if _clean_text(params.get("ven_cd")):
        clauses.append(f"{alias}.Rd03_Ven_Cd = %(ven_cd)s")
        sql_params["ven_cd"] = _clean_text(params.get("ven_cd"))
    if _clean_text(params.get("ven_nm")):
        clauses.append(f"{alias}.Rd03_Ven_Nm LIKE %(ven_nm_like)s")
        sql_params["ven_nm_like"] = like_value(params.get("ven_nm"))
    if _clean_text(params.get("sales_man_nm")):
        clauses.append("SalesMan.Rd06_User_Nm LIKE %(sales_man_nm_like)s")
        sql_params["sales_man_nm_like"] = like_value(params.get("sales_man_nm"))
    if _clean_text(params.get("sido_nm")):
        clauses.append("Road1.Rd021_Sido LIKE %(sido_nm_like)s")
        sql_params["sido_nm_like"] = like_value(params.get("sido_nm"))
    if _clean_text(params.get("gugun_nm")):
        clauses.append("Road1.Rd021_Gugun LIKE %(gugun_nm_like)s")
        sql_params["gugun_nm_like"] = like_value(params.get("gugun_nm"))
    if _clean_text(params.get("road_nm")):
        clauses.append("(Road1.Rd021_RoadNm LIKE %(road_nm_like)s OR V.Rd03_Address LIKE %(road_nm_like)s OR V.Rd03_Address2 LIKE %(road_nm_like)s)")
        sql_params["road_nm_like"] = like_value(params.get("road_nm"))


def _load_rddbc130_monthly(params: Dict[str, Any], policy: Dict[str, Any]) -> pd.DataFrame:
    t0 = time.perf_counter()
    date_from = _clean_text(params.get("date_from"))
    date_to = _clean_text(policy.get("effective_date_to") or policy.get("requested_date_to") or params.get("date_to"))
    if len(date_from) != 8 or len(date_to) != 8:
        return pd.DataFrame()
    sql_params = {"date_from": date_from, "date_to": date_to}
    log.info(
        "[analytics.customer_sales_forecast.source] source=Rddbc130 trans_di=3 date_from=%s date_to=%s evaluation_mode=%s",
        date_from,
        date_to,
        policy.get("evaluation_mode"),
    )
    sql = """
SELECT
    LEFT(LTRIM(RTRIM(T.Rd13_Trans_YyMmDd)), 6) AS 기준월,
    LTRIM(RTRIM(T.Rd13_Ven_Cd)) AS 매출처코드,
    SUM(CAST(COALESCE(T.Rd13_Supply_Price, 0) AS float)) AS 매출공급가액,
    SUM(CAST(COALESCE(T.Rd13_Tax_Price, 0) AS float)) AS 매출세액,
    SUM(CAST(COALESCE(T.Rd13_Tot_Amt, COALESCE(T.Rd13_Supply_Price, 0) + COALESCE(T.Rd13_Tax_Price, 0), 0) AS float)) AS 매출합계,
    SUM(CAST(COALESCE(T.Rd13_Supply_Price, 0) + COALESCE(T.Rd13_Tax_Price, 0) AS float)) AS 계산합계,
    COUNT_BIG(*) AS 집계건수
FROM dbo.Rddbc130 AS T WITH (NOLOCK)
WHERE T.Rd13_Trans_YyMmDd >= %(date_from)s
  AND T.Rd13_Trans_YyMmDd <= %(date_to)s
  AND T.Rd13_Trans_Di = '3'
  AND T.Rd13_Ven_Cd >= '50000'
  AND T.Rd13_Ven_Cd <= '8ZZZZ'
GROUP BY LEFT(LTRIM(RTRIM(T.Rd13_Trans_YyMmDd)), 6), LTRIM(RTRIM(T.Rd13_Ven_Cd))
"""
    df = query_to_df(sql, sql_params)
    elapsed = time.perf_counter() - t0
    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()
    raw_rows = int(pd.to_numeric(df.get("집계건수", 0), errors="coerce").fillna(0).sum()) if not df.empty else 0
    total_supply = float(pd.to_numeric(df.get("매출공급가액", 0), errors="coerce").fillna(0).sum()) if not df.empty else 0
    total_tax = float(pd.to_numeric(df.get("매출세액", 0), errors="coerce").fillna(0).sum()) if not df.empty else 0
    total_amount = float(pd.to_numeric(df.get("매출합계", 0), errors="coerce").fillna(0).sum()) if not df.empty else 0
    calc_amount = float(pd.to_numeric(df.get("계산합계", 0), errors="coerce").fillna(0).sum()) if not df.empty else 0
    log.info(
        "[analytics.customer_sales_forecast.sql] raw_rows=%s monthly_rows=%s elapsed=%.3fs",
        raw_rows,
        len(df),
        elapsed,
    )
    if abs(total_amount - calc_amount) >= 0.5:
        log.info(
            "[analytics.customer_sales_forecast.amount_check] rd13_tot_amt=%.0f supply_plus_tax=%.0f diff=%.0f",
            total_amount,
            calc_amount,
            total_amount - calc_amount,
        )
    df.attrs.update(
        {
            "source_table": "Rddbc130",
            "source_mode": "transaction_statement",
            "trans_di": "3",
            "date_from": date_from,
            "date_to": date_to,
            "raw_rows": raw_rows,
            "monthly_rows": int(len(df)),
            "total_supply": total_supply,
            "total_tax": total_tax,
            "total_amount": total_amount,
            "source_elapsed": elapsed,
        }
    )
    return df


def _load_customer_master(params: Dict[str, Any]) -> pd.DataFrame:
    t0 = time.perf_counter()
    sql_params: Dict[str, Any] = {"dummy": 1}
    clauses = [
        "1 = %(dummy)s",
        "V.Rd03_Ven_Cd >= '50000'",
        "V.Rd03_Ven_Cd <= '8ZZZZ'",
    ]
    _add_customer_filters(clauses, sql_params, params, alias="V")
    where_sql = " AND ".join(clauses)
    sql = f"""
SELECT
    LTRIM(RTRIM(V.Rd03_Ven_Cd)) AS 매출처코드,
    COALESCE(NULLIF(LTRIM(RTRIM(V.Rd03_Ven_Nm)), ''), '(미지정)') AS 매출처명,
    LTRIM(RTRIM(COALESCE(V.Rd03_Sales_Man, ''))) AS 영업사원코드,
    COALESCE(NULLIF(LTRIM(RTRIM(SalesMan.Rd06_User_Nm)), ''), '') AS 담당영업사원명,
    COALESCE(NULLIF(LTRIM(RTRIM(Road1.Rd021_Sido)), ''), '') AS 시도명,
    COALESCE(NULLIF(LTRIM(RTRIM(Road1.Rd021_Gugun)), ''), '') AS 시구군명,
    COALESCE(NULLIF(LTRIM(RTRIM(Road1.Rd021_RoadNm)), ''), '') AS 도로명
FROM dbo.Rddbc030 AS V WITH (NOLOCK)
LEFT JOIN dbo.Rddbc060 AS SalesMan WITH (NOLOCK)
    ON V.Rd03_Sales_Man = SalesMan.Rd06_User_Cd
LEFT JOIN dbo.Rddbc021 AS Road1 WITH (NOLOCK)
    ON LTRIM(RTRIM(Road1.Rd021_RoadCd)) = LTRIM(RTRIM(V.Rd03_RoadCd))
   AND LTRIM(RTRIM(Road1.Rd021_DongSeq)) = LTRIM(RTRIM(V.Rd03_DongSeq))
WHERE {where_sql}
"""
    df = query_to_df(sql, sql_params)
    if isinstance(df, pd.DataFrame) and not df.empty:
        df = df.drop_duplicates("매출처코드", keep="first")
    log.info("[analytics.customer_sales_forecast.sql] source=Rddbc030 raw_rows=%s grouped_rows=%s elapsed=%.3fs", len(df) if isinstance(df, pd.DataFrame) else 0, len(df) if isinstance(df, pd.DataFrame) else 0, time.perf_counter() - t0)
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _has_master_filter(params: Dict[str, Any]) -> bool:
    return any(
        _clean_text(params.get(k))
        for k in ("ven_cd", "ven_nm", "sales_man_nm", "sido_nm", "gugun_nm", "road_nm")
    )


def _combine_sources(params: Dict[str, Any], policy: Dict[str, Any]) -> pd.DataFrame:
    out = _load_rddbc130_monthly(params, policy)
    stats = dict(getattr(out, "attrs", {}) or {})
    if not isinstance(out, pd.DataFrame) or out.empty:
        return pd.DataFrame(columns=["기준월", "매출처코드", "매출공급가액", "매출세액", "매출합계", "집계건수"])
    out["기준월"] = out["기준월"].map(_parse_yyyymm)
    out["매출처코드"] = out["매출처코드"].astype(str).str.strip()
    for c in ["매출공급가액", "매출세액", "매출합계", "계산합계", "집계건수"]:
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0) if c in out.columns else 0
    out = (
        out.groupby(["기준월", "매출처코드"], as_index=False)
        .agg(
            매출공급가액=("매출공급가액", "sum"),
            매출세액=("매출세액", "sum"),
            매출합계=("매출합계", "sum"),
            집계건수=("집계건수", "sum"),
        )
    )
    out.attrs.update(stats)
    return out


def _build_workforward(monthly: pd.DataFrame, params: Dict[str, Any]) -> pd.DataFrame:
    src = monthly[["매출처코드", "기준월", "매출합계"]].rename(
        columns={"매출처코드": "제품코드"}
    )
    metrics = _build_sales_month_workforward_metrics(src, params)
    return metrics.rename(
        columns={
            "제품코드": "매출처코드",
            "월시점 예상매출": "당월 예상매출",
            "월시점 잔여예상": "당월 잔여예상",
            "월시점 달성률": "당월 진척률",
        }
    )


def _trend_counts(df: pd.DataFrame) -> Dict[str, int]:
    if df is None or df.empty or "추세판정" not in df.columns:
        return {"증가": 0, "감소": 0, "안정": 0, "자료부족": 0}
    vals = df["추세판정"].fillna("").astype(str).str.strip()
    return {
        "증가": int((vals == "증가").sum()),
        "감소": int((vals == "감소").sum()),
        "안정": int((vals == "안정").sum()),
        "자료부족": int((~vals.isin(["증가", "감소", "안정"])).sum()),
    }


def _meta_from_df(df: pd.DataFrame, params: Dict[str, Any], policy: Dict[str, Any], months: list[str]) -> Dict[str, Any]:
    labels = _progress_labels(policy)
    attrs = getattr(df, "attrs", {}) or {}
    if df is None or df.empty:
        return {
            "row_count": 0,
            "row_count_total": 0,
            "customer_count": 0,
            "salesperson_count": 0,
            "region_count": 0,
            "month_count": len(months),
            "completed_month_count": 0,
            "current_progress_title": labels["title"],
            "current_sales_label": labels["sales"],
            "current_expected_label": labels["expected"],
            "current_remaining_label": labels["remaining"],
            "current_progress_label": labels["progress"],
            "source_table": attrs.get("source_table") or "Rddbc130",
            "source_mode": attrs.get("source_mode") or "transaction_statement",
            "trans_di": attrs.get("trans_di") or "3",
            "date_from": attrs.get("date_from") or params.get("date_from"),
            "date_to": attrs.get("date_to") or policy.get("effective_date_to") or params.get("date_to"),
            "raw_rows": attrs.get("raw_rows", 0),
            "monthly_rows": attrs.get("monthly_rows", 0),
            "total_supply": attrs.get("total_supply", 0),
            "total_tax": attrs.get("total_tax", 0),
            "total_amount": attrs.get("total_amount", 0),
            "salesperson_distribution": {},
            "province_distribution": {},
            "region_distribution": {},
        }
    def _sum_col(col: str) -> float:
        if col not in df.columns:
            return 0.0
        return float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())

    def _nonempty_text(col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([], dtype="object")
        s = df[col].fillna("").astype(str).str.strip()
        return s[s != ""]

    def _distribution(col: str, limit: int | None = None) -> Dict[str, int]:
        s = _nonempty_text(col)
        if s.empty:
            return {}
        vc = s.value_counts()
        if limit:
            vc = vc.head(limit)
        return {str(k): int(v) for k, v in vc.items()}

    customer_count = int(df["매출처코드"].nunique()) if "매출처코드" in df.columns else int(len(df))
    salesperson_distribution = _distribution("담당영업사원명")
    province_distribution = _distribution("시도명")
    if {"시도명", "시구군명"}.issubset(df.columns):
        region_s = (
            df[["시도명", "시구군명"]]
            .fillna("")
            .astype(str)
            .apply(lambda r: " ".join(x.strip() for x in r if str(x).strip()).strip(), axis=1)
        )
        region_s = region_s[region_s != ""]
        region_distribution = {str(k): int(v) for k, v in region_s.value_counts().items()}
    else:
        region_distribution = {}

    completed_count = int(pd.to_numeric(df["완료월수"], errors="coerce").fillna(0).max()) if "완료월수" in df.columns else 0
    current_col = labels["sales"]
    expected_col = labels["expected"]
    remaining_col = labels["remaining"]
    current_total = _sum_col(current_col)
    expected_total = _sum_col(expected_col)
    remaining_total = _sum_col(remaining_col)
    progress = current_total / expected_total * 100 if abs(expected_total) >= 1e-12 else 0
    forecast_grade_counts = df["예상등급"].fillna("미분류").astype(str).value_counts().to_dict() if "예상등급" in df.columns else {}
    return {
        "row_count": int(len(df)),
        "row_count_total": int(len(df)),
        "customer_count": customer_count,
        "salesperson_count": int(len(salesperson_distribution)),
        "region_count": int(len(region_distribution)),
        "salesperson_distribution": salesperson_distribution,
        "province_distribution": province_distribution,
        "region_distribution": region_distribution,
        "month_count": len(months),
        "completed_month_count": completed_count,
        "sum_supply_amt": _sum_col("총매출공급가액"),
        "sum_tax_amt": _sum_col("총매출세액"),
        "sum_sales_amt": _sum_col("총매출액"),
        "sum_completed_month_sales_amt": _sum_col("완료월총매출"),
        "avg_completed_month_sales_amt": _sum_col("완료월총매출") / completed_count if completed_count > 0 else 0,
        "sum_current_month_sales_amt": current_total,
        "sum_current_month_expected_amt": expected_total,
        "sum_current_month_remaining_expected_amt": remaining_total,
        "current_month_progress_pct": progress,
        "sum_next_month_forecast_amt": _sum_col("다음월예상매출"),
        "sum_3month_forecast_amt": _sum_col("3개월예상매출"),
        "sum_6month_forecast_amt": _sum_col("6개월예상매출"),
        "current_progress_title": labels["title"],
        "current_sales_label": labels["sales"],
        "current_expected_label": labels["expected"],
        "current_remaining_label": labels["remaining"],
        "current_progress_label": labels["progress"],
        "trend_judge_counts": _trend_counts(df),
        "forecast_grade_counts": forecast_grade_counts,
        "evaluation_mode": policy.get("evaluation_mode"),
        "evaluation_month": policy.get("evaluation_month"),
        "source_table": attrs.get("source_table") or "Rddbc130",
        "source_mode": attrs.get("source_mode") or "transaction_statement",
        "trans_di": attrs.get("trans_di") or "3",
        "date_from": attrs.get("date_from") or params.get("date_from"),
        "date_to": attrs.get("date_to") or policy.get("effective_date_to") or params.get("date_to"),
        "raw_rows": attrs.get("raw_rows", 0),
        "monthly_rows": attrs.get("monthly_rows", 0),
        "total_supply": attrs.get("total_supply", 0),
        "total_tax": attrs.get("total_tax", 0),
        "total_amount": attrs.get("total_amount", 0),
    }


def get_customer_sales_forecast_df(params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    t0 = time.perf_counter()
    params = _apply_period_source_policy_params(_apply_month_or_date_params(coalesce_params(params)))
    policy = _resolve_period_source_policy(params)
    months = _month_range(params, policy)
    source_label = SOURCE_LABEL
    source_t0 = time.perf_counter()
    monthly = _combine_sources(params, policy)
    source_stats = dict(getattr(monthly, "attrs", {}) or {})
    source_elapsed = time.perf_counter() - source_t0
    master_t0 = time.perf_counter()
    master = _load_customer_master(params)
    master_elapsed = time.perf_counter() - master_t0
    if monthly.empty:
        return pd.DataFrame()

    customers = sorted(set(monthly["매출처코드"].astype(str)))
    grid = pd.MultiIndex.from_product([customers, months], names=["매출처코드", "기준월"]).to_frame(index=False)
    monthly = grid.merge(monthly, on=["매출처코드", "기준월"], how="left")
    for c in ["매출공급가액", "매출세액", "매출합계", "집계건수"]:
        monthly[c] = pd.to_numeric(monthly.get(c, 0), errors="coerce").fillna(0)
    monthly["분석자료원"] = source_label
    monthly["기간구분"] = _display_period_label(policy)
    forecast_t0 = time.perf_counter()
    wf = _build_workforward(monthly, params)
    evaluation_month = str(policy.get("evaluation_month") or (months[-1] if months else ""))
    eval_wf = wf[wf["기준월"].astype(str) == evaluation_month].copy()

    pivot = (
        monthly.pivot_table(index="매출처코드", columns="기준월", values="매출합계", aggfunc="sum", fill_value=0)
        .reset_index()
    )
    for m in months:
        if m not in pivot.columns:
            pivot[m] = 0
    month_cols = [f"{_fmt_yyyymm_col(m)} 매출" for m in months]
    pivot = pivot.rename(columns={m: f"{_fmt_yyyymm_col(m)} 매출" for m in months})

    base = (
        monthly.groupby("매출처코드", as_index=False)
        .agg(
            총매출공급가액=("매출공급가액", "sum"),
            총매출세액=("매출세액", "sum"),
            총매출액=("매출합계", "sum"),
            총집계건수=("집계건수", "sum"),
            분석자료원=("분석자료원", "first"),
            기간구분=("기간구분", "first"),
        )
    )
    out = base.merge(pivot, on="매출처코드", how="left", validate="one_to_one")
    if not master.empty:
        out = out.merge(master, on="매출처코드", how="inner" if _has_master_filter(params) else "left", validate="many_to_one")
    elif _has_master_filter(params):
        out = out.iloc[0:0].copy()
    for c in ["매출처명", "영업사원코드", "담당영업사원명", "시도명", "시구군명"]:
        if c not in out.columns:
            out[c] = ""
        out[c] = out[c].fillna("")

    completed, current_month, _future = _split_sales_period_months(months, params)
    completed_cols = [f"{_fmt_yyyymm_col(m)} 매출" for m in completed if f"{_fmt_yyyymm_col(m)} 매출" in out.columns]
    current_col = f"{_fmt_yyyymm_col(current_month)} 매출" if current_month else ""
    for c in month_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)
    out["완료월총매출"] = out[completed_cols].sum(axis=1) if completed_cols else 0
    out["월평균매출"] = out["총매출액"] / max(len(months), 1)
    out["완료월수"] = len(completed_cols)
    out["완료월평균매출"] = out["완료월총매출"] / max(len(completed_cols), 1)
    out["당월 현재매출"] = pd.to_numeric(out[current_col], errors="coerce").fillna(0) if current_col in out.columns else 0

    eval_cols = [
        "매출처코드",
        "월시점 완료월수",
        "월시점 완료월평균매출",
        "월시점 최근3개월평균매출",
        "월시점 최근6개월평균매출",
        "월시점 증감률",
        "추세판정",
        "당월 예상매출",
        "당월 잔여예상",
        "당월 진척률",
    ]
    eval_keep = [c for c in eval_cols if c in eval_wf.columns]
    out = out.merge(eval_wf[eval_keep].drop_duplicates("매출처코드"), on="매출처코드", how="left", validate="one_to_one")
    out["최근3개월평균매출"] = pd.to_numeric(out.get("월시점 최근3개월평균매출", 0), errors="coerce").fillna(0)
    out["최근6개월평균매출"] = pd.to_numeric(out.get("월시점 최근6개월평균매출", 0), errors="coerce").fillna(0)
    out["최근3개월증감률"] = pd.to_numeric(out.get("월시점 증감률", 0), errors="coerce").fillna(0)
    out["매출발생월수"] = out[completed_cols].ne(0).sum(axis=1) if completed_cols else 0
    for c in ["당월 예상매출", "당월 잔여예상", "당월 진척률"]:
        out[c] = pd.to_numeric(out.get(c, 0), errors="coerce").fillna(0)

    forecasts = []
    for _, row in out.iterrows():
        base_label, adjusted_rate_pct, next_month = _forecast_projection_from_row(row)
        grade = _forecast_grade(row)
        forecasts.append(
            {
                "예상기준": base_label,
                "적용증감률": adjusted_rate_pct,
                "다음월예상매출": round(next_month, 0),
                "3개월예상매출": round(next_month * 3, 0),
                "6개월예상매출": round(next_month * 6, 0),
                "예상등급": grade,
            }
        )
    out = pd.concat([out.reset_index(drop=True), pd.DataFrame(forecasts)], axis=1)
    forecast_elapsed = time.perf_counter() - forecast_t0

    pre_trend_counts = _trend_counts(out)
    pre_grade_counts = out["예상등급"].fillna("미분류").astype(str).value_counts().to_dict() if "예상등급" in out.columns else {}
    trend_filter = _clean_text(params.get("trend_judge"))
    grade_filter = _clean_text(params.get("forecast_grade"))
    if trend_filter:
        out = out[out["추세판정"].fillna("").astype(str).str.strip() == trend_filter].copy()
    if grade_filter:
        out = out[out["예상등급"].fillna("").astype(str).str.strip() == grade_filter].copy()
    post_trend_counts = _trend_counts(out)
    post_grade_counts = out["예상등급"].fillna("미분류").astype(str).value_counts().to_dict() if "예상등급" in out.columns else {}

    out = out.sort_values(["다음월예상매출", "총매출액", "매출처명", "매출처코드"], ascending=[False, False, True, True]).reset_index(drop=True)
    if "순번" in out.columns:
        out = out.drop(columns=["순번"])
    out.insert(0, "순번", range(1, len(out) + 1))
    label_map = _eval_label_map(policy)
    public_columns = [label_map.get(c, c) for c in CUSTOMER_FORECAST_COLUMNS]
    for internal_col, public_col in label_map.items():
        if public_col != internal_col and internal_col in out.columns:
            out[public_col] = out[internal_col]
            out = out.drop(columns=[internal_col])
    for c in public_columns + month_cols:
        if c not in out.columns:
            out[c] = 0 if c.endswith(("매출", "예상")) or c.endswith("률") else ""
    out = out[public_columns + [c for c in month_cols if c in out.columns]].copy()
    out = _normalize_analytics_numeric_columns(out)
    labels = _progress_labels(policy)
    out.attrs.update(
        {
            "months": months,
            "evaluation_mode": policy.get("evaluation_mode"),
            "evaluation_month": policy.get("evaluation_month"),
            "source_label": source_label,
            "display_period_label": _display_period_label(policy),
            "pre_filter_trend_judge_counts": pre_trend_counts,
            "post_filter_trend_judge_counts": post_trend_counts,
            "pre_filter_forecast_grade_counts": pre_grade_counts,
            "post_filter_forecast_grade_counts": post_grade_counts,
            "current_progress_title": labels["title"],
            "current_sales_label": labels["sales"],
            "current_expected_label": labels["expected"],
            "current_remaining_label": labels["remaining"],
            "current_progress_label": labels["progress"],
            "source_table": source_stats.get("source_table") or "Rddbc130",
            "source_mode": source_stats.get("source_mode") or "transaction_statement",
            "trans_di": source_stats.get("trans_di") or "3",
            "date_from": source_stats.get("date_from") or params.get("date_from"),
            "date_to": source_stats.get("date_to") or policy.get("effective_date_to") or params.get("date_to"),
            "raw_rows": source_stats.get("raw_rows", 0),
            "monthly_rows": source_stats.get("monthly_rows", 0),
            "total_supply": source_stats.get("total_supply", 0),
            "total_tax": source_stats.get("total_tax", 0),
            "total_amount": source_stats.get("total_amount", 0),
        }
    )
    finish_elapsed = time.perf_counter() - t0 - source_elapsed - master_elapsed - forecast_elapsed
    log.info(
        "[analytics.customer_sales_forecast.perf] monthly_rows=%s customer_count=%s source_elapsed=%.3fs master_elapsed=%.3fs forecast_elapsed=%.3fs finish_elapsed=%.3fs total_elapsed=%.3fs",
        len(monthly),
        out["매출처코드"].nunique() if "매출처코드" in out.columns else 0,
        source_elapsed,
        master_elapsed,
        forecast_elapsed,
        max(finish_elapsed, 0),
        time.perf_counter() - t0,
    )
    return out


def get_customer_sales_forecast_result(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = _apply_month_or_date_params(coalesce_params(params))
    df = get_customer_sales_forecast_df(params)
    rows = 0 if df is None else int(len(df))
    attrs = getattr(df, "attrs", {}) or {}
    source_label = str(attrs.get("source_label") or SOURCE_LABEL)
    query_summary = _fmt_analytics_query_summary(params, source_label)
    policy = _resolve_period_source_policy(_apply_period_source_policy_params(params.copy()))
    meta = _meta_from_df(df, params, policy, list(attrs.get("months") or []))
    meta.update(
        {
            "analytics": True,
            "analysis_type": "customer_sales_forecast",
            "summary_type": "customer_forecast",
            "group_type": "customer",
            "source_label": source_label,
            "display_period_label": attrs.get("display_period_label"),
            "query_summary": query_summary,
            "condition": query_summary,
            "pre_filter_trend_judge_counts": attrs.get("pre_filter_trend_judge_counts") or {},
            "post_filter_trend_judge_counts": attrs.get("post_filter_trend_judge_counts") or {},
            "pre_filter_forecast_grade_counts": attrs.get("pre_filter_forecast_grade_counts") or {},
            "post_filter_forecast_grade_counts": attrs.get("post_filter_forecast_grade_counts") or {},
            "summary_md": (
                f"매출처별 매출 예상: 조회조건 {query_summary} / "
                f"매출처수 {_fmt_num_for_summary(meta.get('customer_count'))} / "
                f"완료월평균매출 {_fmt_num_for_summary(meta.get('avg_completed_month_sales_amt'))} / "
                f"{str(meta.get('current_sales_label') or '당월 현재매출').replace(' ', '')} {_fmt_num_for_summary(meta.get('sum_current_month_sales_amt'))} / "
                f"다음월예상매출 {_fmt_num_for_summary(meta.get('sum_next_month_forecast_amt'))} / "
                f"추세판정 {_fmt_counts_for_summary(meta.get('trend_judge_counts') or {})} / "
                f"자료원 {source_label}"
            ),
        }
    )
    payload = build_result_payload(
        table=TABLE,
        title="매출처별 매출 예상",
        action="매출처별 매출 예상",
        params=params,
        df=df,
        message=f"매출처별 매출 예상 {rows:,}건",
    )
    payload.setdefault("meta", {})
    payload["meta"].update(meta)
    return payload

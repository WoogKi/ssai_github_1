# app/services/analytics_manufacturer_sales_trend_service.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import pandas as pd

from app.services.rddbc_io_common import build_result_payload, coalesce_params
from app.services.analytics_sales_trend_service import (
    TABLE,
    _apply_month_or_date_params,
    _apply_period_source_policy_params,
    _count_text_values,
    _effective_source_label,
    _fmt_analytics_query_summary,
    _fmt_counts_for_summary,
    _fmt_num_for_summary,
    _fmt_yyyymm_col,
    _iter_yyyymm,
    _build_sales_month_workforward_metrics,
    _normalize_analytics_numeric_columns,
    _parse_yyyymm,
    _pct_change,
    _resolve_period_source_policy,
    _resolve_source_mode,
    _safe_analytics_log_meta,
    _split_sales_period_months,
    get_sales_trend_df,
)

log = logging.getLogger("ssai.sims.analytics_manufacturer_sales_trend")


MANUFACTURER_TREND_COLUMNS = [
    "순번",
    "기준월",
    "제약사명",
    "매출공급가액",
    "매출세액",
    "매출합계",
    "집계건수",
    "제품수",
    "매입처수",
    "분석자료원",
    "기간구분",
    "전월대비매출",
    "전월대비매출증감률",
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
    "월시점 최근3개월증감률",
    "추세판정",
    "판정결과",
]

MANUFACTURER_SUMMARY_COLUMNS = [
    "순번",
    "제약사명",
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
    "제품수",
    "매입처수",
    "총집계건수",
    "분석자료원",
    "기간구분",
]

MANUFACTURER_FORBIDDEN_TOKENS = ("수량", "다음월예상매출", "예상등급")


def _manufacturer_summary_current_sales_column(policy: Optional[Dict[str, Any]]) -> str:
    mode = str((policy or {}).get("evaluation_mode") or "")
    return "당월 현재매출" if mode == "current_monthly" else "평가월 매출"


def _manufacturer_summary_eval_label_map(policy: Optional[Dict[str, Any]]) -> dict[str, str]:
    prefix = "당월" if str((policy or {}).get("evaluation_mode") or "") == "current_monthly" else "평가월"
    return {
        "당월 현재매출": f"{prefix} 현재매출" if prefix == "당월" else "평가월 매출",
        "당월 예상매출": f"{prefix} 예상매출",
        "당월 잔여예상": f"{prefix} 잔여예상",
        "당월 진척률": f"{prefix} 진척률",
    }


def _manufacturer_summary_public_columns(policy: Optional[Dict[str, Any]]) -> list[str]:
    label_map = _manufacturer_summary_eval_label_map(policy)
    return [label_map.get(c, c) for c in MANUFACTURER_SUMMARY_COLUMNS]


def _display_period_label(value: Any) -> str:
    code = str(value or "").strip()
    return {
        "current_monthly": "현재월 월집계",
        "historical_midmonth": "과거 월중",
        "historical_month_end": "과거 월말",
    }.get(code, code)


def _fmt_month_label(value: Any) -> str:
    m = _parse_yyyymm(value)
    if len(m) == 6:
        return f"{m[:4]}-{m[4:6]}"
    return str(value or "").strip()


def _completed_month_range_label(months: list[str], params: Dict[str, Any]) -> str:
    completed, _current, _future = _split_sales_period_months(months, params)
    if not completed:
        return "완료월 없음"
    return f"완료월 {_fmt_month_label(completed[0])}~{_fmt_month_label(completed[-1])}"


def _manufacturer_summary_period_caption(months: list[str], params: Dict[str, Any], policy: Dict[str, Any]) -> str:
    completed_label = _completed_month_range_label(months, params)
    evaluation_month = str(policy.get("evaluation_month") or "")
    evaluation_label = _fmt_month_label(evaluation_month)
    mode = str(policy.get("evaluation_mode") or "")
    display_label = _display_period_label(mode)
    if mode == "current_monthly":
        return f"{completed_label} │ 당월 {evaluation_label} │ 현재월 월집계"
    if mode == "historical_midmonth":
        date_to = str(policy.get("effective_date_to") or policy.get("requested_date_to") or "")
        day_label = f"{date_to[4:6]}-{date_to[6:8]}까지" if len(date_to) == 8 else "종료일까지"
        return f"{completed_label} │ 평가월 {evaluation_label}({day_label}) │ 과거 월중 상세"
    if mode == "historical_month_end":
        return f"{completed_label} │ 평가월 {evaluation_label} │ 과거 월말 월집계"
    return f"{completed_label} │ 평가월 {evaluation_label} │ {display_label}"


def _manufacturer_detail_period_caption(months: list[str], policy: Dict[str, Any]) -> str:
    if not months:
        return "조회기간: -"
    final_state = "부분월" if bool(policy.get("use_hybrid_detail") or policy.get("use_hybrid")) else "월집계"
    return f"조회기간: {_fmt_month_label(months[0])} ~ {_fmt_month_label(months[-1])} │ 최종월 상태: {final_state}"


def _period_log_values(action: str, params: Dict[str, Any], months: list[str], policy: Dict[str, Any]) -> None:
    completed, current_month, _future = _split_sales_period_months(months, params)
    current_month_source = "detail" if bool(policy.get("use_hybrid_detail") or policy.get("use_hybrid")) else "monthly"
    log.info(
        "[analytics.manufacturer_sales_period] action=%s evaluation_mode=%s evaluation_month=%s completed_month_from=%s completed_month_to=%s completed_month_count=%s current_month_source=%s display_period_label=%s",
        action,
        policy.get("evaluation_mode") or "",
        policy.get("evaluation_month") or current_month or "",
        completed[0] if completed else "",
        completed[-1] if completed else "",
        len(completed),
        current_month_source,
        _display_period_label(policy.get("evaluation_mode")),
    )


def _normalize_manufacturer_name(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "제약사 미지정"


def _month_list(params: Dict[str, Any], raw: pd.DataFrame) -> list[str]:
    month_from = _parse_yyyymm(params.get("month_from"))
    month_to = _parse_yyyymm(params.get("month_to"))
    if month_from and month_to:
        return _iter_yyyymm(month_from, month_to)
    if raw is not None and not raw.empty and "기준월" in raw.columns:
        return sorted({_parse_yyyymm(v) for v in raw["기준월"].dropna().tolist() if _parse_yyyymm(v)})
    return []


def _dashboard_display_months(params: Dict[str, Any]) -> list[str]:
    """Return Dashboard's explicit completed-month display range, if present."""
    month_from = _parse_yyyymm(params.get("dashboard_lite_display_month_from"))
    month_to = _parse_yyyymm(params.get("dashboard_lite_display_month_to"))
    return _iter_yyyymm(month_from, month_to) if month_from and month_to else []


def _period_label_map(months: list[str], params: Dict[str, Any]) -> dict[str, str]:
    policy = _resolve_period_source_policy(params)
    evaluation_month = str(policy.get("evaluation_month") or "")
    is_partial = bool(policy.get("use_hybrid_detail") or policy.get("use_hybrid"))
    labels: dict[str, str] = {}
    for m in months:
        if is_partial and evaluation_month and str(m) == evaluation_month:
            labels[m] = "부분월"
        else:
            labels[m] = "월집계"
    return labels


def _trend_judge_from_row(row: pd.Series) -> str:
    completed_count = float(row.get("월시점 완료월수") or row.get("완료월수") or 0)
    total = float(row.get("_완료월총매출") or row.get("완료월총매출") or 0)
    current_sales = float(row.get("당월 현재매출") or row.get("매출공급가액") or 0)
    r3 = float(row.get("최근3개월평균매출") or row.get("월시점 최근3개월평균매출") or 0)
    r6 = float(row.get("최근6개월평균매출") or row.get("월시점 최근6개월평균매출") or 0)
    if total <= 0 and current_sales > 0:
        return "비교자료 부족"
    if completed_count <= 0:
        return "자료부족"
    if total < 0:
        return "반품주의"
    if completed_count < 3:
        return "신규/증가" if total > 0 else "자료부족"
    change = _pct_change(r3, r6)
    if change >= 10:
        return "증가"
    if change <= -10:
        return "감소"
    return "안정"


def _clean_public_columns(df: pd.DataFrame, columns: list[str], dynamic_cols: Optional[list[str]] = None) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    dynamic_cols = dynamic_cols or []
    keep = [c for c in columns if c in df.columns]
    keep += [c for c in dynamic_cols if c in df.columns and c not in keep]
    # Dynamic monthly columns are public only when explicitly supplied by the
    # caller.  This keeps Dashboard's evaluation/support months out of the
    # displayed completed-month table.
    def _is_dynamic_month_column(column: Any) -> bool:
        text = str(column)
        return len(text) == 10 and text[4:5] == "-" and text[7:8] == " " and text[:4].isdigit() and text[5:7].isdigit() and text.endswith("매출")

    rest = [
        c for c in df.columns
        if c not in keep
        and not _is_dynamic_month_column(c)
        and not any(token in str(c) for token in MANUFACTURER_FORBIDDEN_TOKENS)
        and not str(c).endswith(("_x", "_y"))
        and not str(c).startswith("_")
        and c not in {"제품코드", "제품명", "규격", "출고수량", "출고할증수량", "총출고수량", "총출고할증수량"}
    ]
    out = df[keep + rest].copy()
    return out


def _prepare_manufacturer_monthly(raw: pd.DataFrame, params: Dict[str, Any]) -> tuple[pd.DataFrame, list[str], Dict[str, Any]]:
    work = raw.copy()
    work["기준월"] = work["기준월"].map(_parse_yyyymm)
    work["제약사명"] = work["제조사명"].map(_normalize_manufacturer_name)
    for c in ["매출공급가액", "매출세액", "매출합계", "집계건수"]:
        if c in work.columns:
            work[c] = pd.to_numeric(work[c], errors="coerce").fillna(0)
        else:
            work[c] = 0

    months = _month_list(params, work)
    groups = sorted({_normalize_manufacturer_name(v) for v in work["제약사명"].dropna().tolist()})
    idx = pd.MultiIndex.from_product([groups, months], names=["제약사명", "기준월"])

    agg = (
        work.groupby(["제약사명", "기준월"], dropna=False)
        .agg(
            매출공급가액=("매출공급가액", "sum"),
            매출세액=("매출세액", "sum"),
            매출합계=("매출합계", "sum"),
            집계건수=("집계건수", "sum"),
            제품수=("제품코드", "nunique") if "제품코드" in work.columns else ("제약사명", "size"),
            매입처수=("매입처코드", "nunique") if "매입처코드" in work.columns else ("제약사명", "size"),
        )
        .reindex(idx, fill_value=0)
        .reset_index()
    )
    source_label = _effective_source_label(_resolve_source_mode(params), raw)
    agg["분석자료원"] = source_label
    labels = _period_label_map(months, params)
    agg["기간구분"] = agg["기준월"].map(lambda m: labels.get(str(m), "완료월"))
    policy = _resolve_period_source_policy(params)
    display_months = _dashboard_display_months(params)
    if display_months:
        evaluation_month = str(policy.get("evaluation_month") or "")
        output_months = list(display_months)
        if evaluation_month and evaluation_month not in output_months:
            output_months.append(evaluation_month)
        return agg, output_months, policy
    return agg, months, policy


def _add_manufacturer_month_metrics(monthly: pd.DataFrame, params: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    if monthly is None or monthly.empty:
        return monthly
    params = params or {}
    out = monthly.copy().sort_values(["제약사명", "기준월"]).reset_index(drop=True)
    grp = out.groupby("제약사명", dropna=False)
    sales = pd.to_numeric(out["매출공급가액"], errors="coerce").fillna(0)
    out["전월대비매출"] = grp["매출공급가액"].diff().fillna(0)
    prev_sales = grp["매출공급가액"].shift(1).fillna(0)
    out["전월대비매출증감률"] = [
        _pct_change(cur, prev) if abs(float(prev or 0)) >= 1e-12 else 0
        for cur, prev in zip(sales.tolist(), prev_sales.tolist())
    ]
    out["_이전월매출"] = grp["매출공급가액"].shift(1)
    out["월시점 완료월수"] = grp.cumcount()
    out["_완료월총매출"] = grp["매출공급가액"].cumsum().shift(1)
    out["_완료월총매출"] = out["_완료월총매출"].where(out["월시점 완료월수"] > 0, 0).fillna(0)
    out["월시점 완료월평균매출"] = (
        out["_완료월총매출"] / out["월시점 완료월수"].replace(0, 1)
    ).where(out["월시점 완료월수"] > 0, 0)
    out["월시점 최근3개월평균매출"] = (
        out.groupby("제약사명", dropna=False)["_이전월매출"]
        .rolling(3, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
        .fillna(0)
    )
    out["월시점 최근6개월평균매출"] = (
        out.groupby("제약사명", dropna=False)["_이전월매출"]
        .rolling(6, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
        .fillna(0)
    )
    out["최근3개월평균매출"] = out["월시점 최근3개월평균매출"]
    out["최근6개월평균매출"] = out["월시점 최근6개월평균매출"]
    out["월시점 최근3개월증감률"] = [
        _pct_change(r3, r6)
        for r3, r6 in zip(out["월시점 최근3개월평균매출"].tolist(), out["월시점 최근6개월평균매출"].tolist())
    ]
    helper_src = out[["제약사명", "기준월", "매출공급가액"]].copy()
    helper_src = helper_src.rename(columns={"제약사명": "제품코드", "매출공급가액": "매출합계"})
    helper_src["출고수량"] = 0
    workforward = _build_sales_month_workforward_metrics(helper_src, params)
    if workforward is not None and not workforward.empty:
        workforward = workforward.rename(columns={"제품코드": "제약사명"})
        metric_cols = [
            "제약사명",
            "기준월",
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
        drop_cols = [c for c in metric_cols if c not in {"제약사명", "기준월"} and c in out.columns]
        if drop_cols:
            out = out.drop(columns=drop_cols)
        out = out.merge(
            workforward[[c for c in metric_cols if c in workforward.columns]],
            on=["제약사명", "기준월"],
            how="left",
            validate="one_to_one",
        )
        out["월시점 최근3개월증감률"] = out["월시점 증감률"]
    else:
        out["월시점 증감률"] = out["월시점 최근3개월증감률"]
        out["월시점 추세판정"] = out.apply(_trend_judge_from_row, axis=1)
        out["월시점 판정결과"] = out["월시점 추세판정"]
        out["월시점 실제매출"] = out["매출공급가액"]
        out["월시점 예상기준"] = "자료부족"
        out["월시점 적용증감률"] = 0
        out["월시점 예상매출"] = 0
        out["월시점 예상대비차이"] = out["월시점 실제매출"]
        out["월시점 잔여예상"] = 0
        out["월시점 달성률"] = 0
        out["추세판정"] = out["월시점 추세판정"]
        out["판정결과"] = out["월시점 판정결과"]
    return out.drop(columns=[c for c in ["_이전월매출"] if c in out.columns])


def _drop_zero_manufacturer_public_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    zero_check_cols = [
        "매출공급가액",
        "매출세액",
        "매출합계",
        "집계건수",
        "월시점 실제매출",
        "월시점 예상매출",
        "월시점 예상대비차이",
        "월시점 잔여예상",
    ]
    check_cols = [c for c in zero_check_cols if c in df.columns]
    if not check_cols:
        return df
    work = df.copy()
    numeric = work[check_cols].apply(pd.to_numeric, errors="coerce").fillna(0)
    keep_mask = numeric.abs().sum(axis=1).gt(0)
    internal_rows = int(len(work))
    out = work.loc[keep_mask].copy()
    public_rows = int(len(out))
    log.info(
        "[analytics.manufacturer_sales_trend.zero_rows] internal_rows=%s public_rows=%s dropped_zero_rows=%s",
        internal_rows,
        public_rows,
        internal_rows - public_rows,
    )
    return out


def _numeric_series(df: pd.DataFrame, column: str, default: float = 0.0) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    if column in df.columns:
        return pd.to_numeric(df[column], errors="coerce").fillna(default)
    return pd.Series([default] * len(df), index=df.index, dtype="float64")


def _numeric_sum(df: pd.DataFrame, column: str) -> float:
    if df is None or df.empty:
        return 0.0
    return float(_numeric_series(df, column).sum())


def _manufacturer_judge_bucket(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"증가", "신규/증가"}:
        return "증가"
    if text == "감소":
        return "감소"
    if text == "안정":
        return "안정"
    return "자료부족"


def _manufacturer_trend_count_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "제약사명" not in df.columns:
        return pd.DataFrame()
    work = df.copy()
    if "기준월" in work.columns:
        work["_기준월_sort"] = work["기준월"].astype(str)
        work = work.sort_values(["제약사명", "_기준월_sort"], ascending=[True, True])
        work = work.drop_duplicates("제약사명", keep="last").drop(columns=["_기준월_sort"], errors="ignore")
    else:
        work = work.drop_duplicates("제약사명", keep="last")
    return work


def _manufacturer_trend_judge_counts(df: pd.DataFrame) -> Dict[str, int]:
    counts = {"증가": 0, "감소": 0, "안정": 0, "자료부족": 0}
    work = _manufacturer_trend_count_rows(df)
    if work.empty or "추세판정" not in work.columns:
        counts["자료부족"] = int(len(work))
        return counts
    for value in work["추세판정"].tolist():
        counts[_manufacturer_judge_bucket(value)] += 1
    return counts


def _manufacturer_evaluation_rows(df: pd.DataFrame, policy: Dict[str, Any]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if "기준월" in df.columns:
        evaluation_month = str(policy.get("evaluation_month") or "")
        if evaluation_month:
            rows = df[df["기준월"].astype(str) == evaluation_month].copy()
            if not rows.empty:
                return rows.drop_duplicates("제약사명", keep="last") if "제약사명" in rows.columns else rows
        return _manufacturer_trend_count_rows(df)
    return df.drop_duplicates("제약사명", keep="last") if "제약사명" in df.columns else df.copy()


def _manufacturer_progress_labels(policy: Dict[str, Any]) -> Dict[str, str]:
    if str(policy.get("evaluation_mode") or "") == "current_monthly":
        return {
            "title": "당월 진행 요약",
            "sales": "당월 현재매출",
            "expected": "당월 예상매출",
            "remaining": "당월 잔여예상",
            "progress": "당월 진척률",
        }
    return {
        "title": "평가월 진행 요약",
        "sales": "평가월 매출",
        "expected": "평가월 예상매출",
        "remaining": "평가월 잔여예상",
        "progress": "평가월 진척률",
    }


def _manufacturer_trend_meta(df: pd.DataFrame, *, months: list[str], policy: Dict[str, Any]) -> Dict[str, Any]:
    if df is None or df.empty:
        labels = _manufacturer_progress_labels(policy)
        return {
            "row_count": 0,
            "row_count_total": 0,
            "manufacturer_count": 0,
            "month_count": len(months),
            "completed_month_count": 0,
            "sum_supply_amt": 0,
            "sum_tax_amt": 0,
            "sum_sales_amt": 0,
            "avg_month_sales_amt": 0,
            "sum_completed_month_sales_amt": 0,
            "avg_completed_month_sales_amt": 0,
            "sum_current_month_sales_amt": 0,
            "sum_current_month_expected_amt": 0,
            "sum_current_month_remaining_expected_amt": 0,
            "current_month_progress_pct": 0,
            "current_progress_title": labels["title"],
            "current_sales_label": labels["sales"],
            "current_expected_label": labels["expected"],
            "current_remaining_label": labels["remaining"],
            "current_progress_label": labels["progress"],
            "trend_judge_counts": {"증가": 0, "감소": 0, "안정": 0, "자료부족": 0},
        }
    total = _numeric_sum(df, "매출공급가액")
    tax_total = _numeric_sum(df, "매출세액")
    sales_total = _numeric_sum(df, "매출합계")
    month_count = max(len(months), 1)
    eval_rows = _manufacturer_evaluation_rows(df, policy)
    completed_counts = _numeric_series(eval_rows, "월시점 완료월수")
    completed_month_count = int(completed_counts.max()) if not completed_counts.empty else 0
    completed_total = float(
        (_numeric_series(eval_rows, "월시점 완료월평균매출") * _numeric_series(eval_rows, "월시점 완료월수")).sum()
    ) if not eval_rows.empty else 0.0
    actual_total = _numeric_sum(eval_rows, "월시점 실제매출")
    expected_total = _numeric_sum(eval_rows, "월시점 예상매출")
    remaining_total = _numeric_sum(eval_rows, "월시점 잔여예상")
    progress_pct = (actual_total / expected_total * 100) if abs(expected_total) >= 1e-12 else 0
    labels = _manufacturer_progress_labels(policy)
    product_count = int(_numeric_series(eval_rows, "제품수").sum()) if not eval_rows.empty else 0
    purchase_count = int(_numeric_series(eval_rows, "매입처수").sum()) if not eval_rows.empty else 0
    return {
        "row_count": int(len(df)),
        "row_count_total": int(len(df)),
        "manufacturer_count": int(df["제약사명"].nunique()) if "제약사명" in df.columns else 0,
        "month_count": len(months),
        "completed_month_count": completed_month_count,
        "sum_supply_amt": total,
        "sum_tax_amt": tax_total,
        "sum_sales_amt": sales_total,
        "product_count": product_count,
        "buy_vendor_count": purchase_count,
        "purchase_vendor_count": purchase_count,
        "avg_month_sales_amt": total / month_count,
        "sum_completed_month_sales_amt": completed_total,
        "avg_completed_month_sales_amt": completed_total / completed_month_count if completed_month_count > 0 else 0,
        "sum_current_month_sales_amt": actual_total,
        "sum_current_month_expected_amt": expected_total,
        "sum_current_month_remaining_expected_amt": remaining_total,
        "current_month_progress_pct": progress_pct,
        "current_progress_title": labels["title"],
        "current_sales_label": labels["sales"],
        "current_expected_label": labels["expected"],
        "current_remaining_label": labels["remaining"],
        "current_progress_label": labels["progress"],
        "trend_judge_counts": _manufacturer_trend_judge_counts(df),
        "evaluation_mode": policy.get("evaluation_mode"),
        "evaluation_month": policy.get("evaluation_month"),
    }


def _manufacturer_summary_meta(df: pd.DataFrame, *, months: list[str], policy: Dict[str, Any]) -> Dict[str, Any]:
    if df is None or df.empty:
        labels = _manufacturer_progress_labels(policy)
        return {
            "row_count": 0,
            "row_count_total": 0,
            "manufacturer_count": 0,
            "month_count": len(months),
            "completed_month_count": 0,
            "sum_supply_amt": 0,
            "sum_tax_amt": 0,
            "sum_sales_amt": 0,
            "product_count": 0,
            "buy_vendor_count": 0,
            "purchase_vendor_count": 0,
            "avg_month_sales_amt": 0,
            "sum_completed_month_sales_amt": 0,
            "avg_completed_month_sales_amt": 0,
            "sum_current_month_sales_amt": 0,
            "sum_current_month_expected_amt": 0,
            "sum_current_month_remaining_expected_amt": 0,
            "current_month_progress_pct": 0,
            "current_progress_title": labels["title"],
            "current_sales_label": labels["sales"],
            "current_sales_column": _manufacturer_summary_current_sales_column(policy),
            "current_expected_label": labels["expected"],
            "current_remaining_label": labels["remaining"],
            "current_progress_label": labels["progress"],
            "trend_judge_counts": {"증가": 0, "감소": 0, "안정": 0, "자료부족": 0},
            "no_current_sales_group_count": 0,
        }
    completed_month_count = int(pd.to_numeric(df.get("완료월수", pd.Series([0])), errors="coerce").fillna(0).max())
    completed_total = float(pd.to_numeric(df.get("완료월총매출", 0), errors="coerce").fillna(0).sum())
    total_supply = float(pd.to_numeric(df.get("총매출공급가액", 0), errors="coerce").fillna(0).sum())
    total_tax = float(pd.to_numeric(df.get("총매출세액", 0), errors="coerce").fillna(0).sum())
    total_sales = float(pd.to_numeric(df.get("총매출액", 0), errors="coerce").fillna(0).sum())
    current_col = "당월 현재매출" if "당월 현재매출" in df.columns else ("평가월 매출" if "평가월 매출" in df.columns else "")
    current_total = float(pd.to_numeric(df.get(current_col, 0), errors="coerce").fillna(0).sum()) if current_col else 0
    expected_col = "당월 예상매출" if "당월 예상매출" in df.columns else ("평가월 예상매출" if "평가월 예상매출" in df.columns else "")
    remaining_col = "당월 잔여예상" if "당월 잔여예상" in df.columns else ("평가월 잔여예상" if "평가월 잔여예상" in df.columns else "")
    expected_total = _numeric_sum(df, expected_col) if expected_col else 0
    remaining_total = _numeric_sum(df, remaining_col) if remaining_col else 0
    progress_pct = (current_total / expected_total * 100) if abs(expected_total) >= 1e-12 else 0
    labels = _manufacturer_progress_labels(policy)
    product_count = int(_numeric_series(df, "제품수").sum())
    purchase_count = int(_numeric_series(df, "매입처수").sum())
    return {
        "row_count": int(len(df)),
        "row_count_total": int(len(df)),
        "manufacturer_count": int(df["제약사명"].nunique()) if "제약사명" in df.columns else int(len(df)),
        "month_count": len(months),
        "completed_month_count": completed_month_count,
        "sum_supply_amt": total_supply,
        "sum_tax_amt": total_tax,
        "sum_sales_amt": total_sales,
        "product_count": product_count,
        "buy_vendor_count": purchase_count,
        "purchase_vendor_count": purchase_count,
        "avg_month_sales_amt": total_supply / max(len(months), 1),
        "sum_completed_month_sales_amt": completed_total,
        "avg_completed_month_sales_amt": completed_total / completed_month_count if completed_month_count > 0 else 0,
        "sum_current_month_sales_amt": current_total,
        "sum_current_month_expected_amt": expected_total,
        "sum_current_month_remaining_expected_amt": remaining_total,
        "current_month_progress_pct": progress_pct,
        "current_progress_title": labels["title"],
        "current_sales_label": labels["sales"],
        "current_sales_column": current_col or labels["sales"],
        "current_expected_label": labels["expected"],
        "current_remaining_label": labels["remaining"],
        "current_progress_label": labels["progress"],
        "trend_judge_counts": _manufacturer_trend_judge_counts(df),
        "no_current_sales_group_count": int((pd.to_numeric(df.get(current_col, 0), errors="coerce").fillna(0) == 0).sum()) if current_col else int(len(df)),
        "evaluation_mode": policy.get("evaluation_mode"),
        "evaluation_month": policy.get("evaluation_month"),
    }


def get_manufacturer_sales_trend(
    params: Optional[Dict[str, Any]] = None,
    raw_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    t0 = time.perf_counter()
    params = _apply_period_source_policy_params(_apply_month_or_date_params(coalesce_params(params)))
    raw = raw_df if raw_df is not None else get_sales_trend_df(params)
    t_source = time.perf_counter()
    if raw is None or raw.empty:
        log.info(
            "[analytics.manufacturer_sales_trend.perf] raw_rows=0 month_rows=0 group_count=0 source=%.3fs group=0.000s calc=0.000s finish=0.000s total=%.3fs",
            t_source - t0,
            time.perf_counter() - t0,
        )
        return pd.DataFrame()
    monthly, months, policy = _prepare_manufacturer_monthly(raw, params)
    t_group = time.perf_counter()
    _period_log_values("trend", params, months, policy)
    out = _add_manufacturer_month_metrics(monthly, params)
    if _dashboard_display_months(params):
        out = out[out["기준월"].astype(str).isin(months)].copy()
    t_calc = time.perf_counter()
    out = _drop_zero_manufacturer_public_rows(out)
    out = out.sort_values(["제약사명", "기준월"], ascending=[True, True]).reset_index(drop=True)
    top = int(params.get("top") or 0)
    if top > 0:
        out = out.head(top).copy()
    if "순번" in out.columns:
        out = out.drop(columns=["순번"])
    out.insert(0, "순번", range(1, len(out) + 1))
    attrs = dict(getattr(raw, "attrs", {}) or {})
    dynamic_cols: list[str] = []
    out = _clean_public_columns(out, MANUFACTURER_TREND_COLUMNS, dynamic_cols)
    out = _normalize_analytics_numeric_columns(out)
    out.attrs.update(attrs)
    out.attrs["evaluation_mode"] = policy.get("evaluation_mode")
    out.attrs["evaluation_month"] = policy.get("evaluation_month")
    out.attrs["source_label"] = _effective_source_label(_resolve_source_mode(params), raw)
    out.attrs["display_period_label"] = _display_period_label(policy.get("evaluation_mode"))
    out.attrs["period_caption"] = _manufacturer_detail_period_caption(months, policy)
    out.attrs["months"] = months
    t_finish = time.perf_counter()
    log.info(
        "[analytics.manufacturer_sales_trend.perf] raw_rows=%s month_rows=%s group_count=%s source=%.3fs group=%.3fs calc=%.3fs finish=%.3fs total=%.3fs",
        len(raw), len(monthly), out["제약사명"].nunique() if "제약사명" in out.columns else 0,
        t_source - t0, t_group - t_source, t_calc - t_group, t_finish - t_calc, t_finish - t0,
    )
    return out


def get_manufacturer_sales_trend_summary(
    params: Optional[Dict[str, Any]] = None,
    raw_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    t0 = time.perf_counter()
    params = _apply_period_source_policy_params(_apply_month_or_date_params(coalesce_params(params)))
    detail = get_manufacturer_sales_trend(params, raw_df=raw_df)
    t_source = time.perf_counter()
    if detail is None or detail.empty:
        log.info(
            "[analytics.manufacturer_sales_trend_summary.perf] raw_rows=0 month_rows=0 group_count=0 source=%.3fs group=0.000s pivot=0.000s calc=0.000s finish=0.000s total=%.3fs",
            t_source - t0,
            time.perf_counter() - t0,
        )
        return pd.DataFrame()
    work = detail.copy()
    months = list(getattr(detail, "attrs", {}).get("months") or sorted({_parse_yyyymm(v) for v in work["기준월"].tolist() if _parse_yyyymm(v)}))
    policy = _resolve_period_source_policy(params)
    dashboard_display_months = _dashboard_display_months(params)
    public_months = dashboard_display_months or months
    _period_log_values("summary", params, months, policy)
    t_group = time.perf_counter()

    pivot = (
        work.pivot_table(index="제약사명", columns="기준월", values="매출공급가액", aggfunc="sum", fill_value=0)
        .reset_index()
    )
    for m in months:
        if m not in pivot.columns:
            pivot[m] = 0
    if months:
        pivot = pivot[["제약사명"] + months]
    month_cols = [f"{_fmt_yyyymm_col(m)} 매출" for m in public_months]
    pivot = pivot.rename(columns={m: f"{_fmt_yyyymm_col(m)} 매출" for m in months})
    t_pivot = time.perf_counter()

    base_work = work[work["기준월"].astype(str).isin(public_months)].copy() if dashboard_display_months else work
    base = (
        base_work.groupby("제약사명", as_index=False)
        .agg(
            총매출공급가액=("매출공급가액", "sum"),
            총매출세액=("매출세액", "sum"),
            총매출액=("매출합계", "sum"),
            제품수=("제품수", "max"),
            매입처수=("매입처수", "max"),
            총집계건수=("집계건수", "sum"),
            분석자료원=("분석자료원", "first"),
        )
    )
    out = base.merge(pivot, on="제약사명", how="outer", validate="one_to_one").fillna(0)
    completed, current_month, _future = _split_sales_period_months(months, params)
    completed_cols = [f"{_fmt_yyyymm_col(m)} 매출" for m in completed if f"{_fmt_yyyymm_col(m)} 매출" in out.columns]
    current_col = f"{_fmt_yyyymm_col(current_month)} 매출" if current_month else ""
    for c in month_cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)
    completed_count = len(completed_cols)
    out["완료월총매출"] = out[completed_cols].sum(axis=1) if completed_cols else 0
    out["월평균매출"] = out["총매출공급가액"] / max(len(months), 1)
    out["완료월수"] = completed_count
    out["완료월평균매출"] = out["완료월총매출"] / max(completed_count, 1)
    out["당월 현재매출"] = pd.to_numeric(out[current_col], errors="coerce").fillna(0) if current_col in out.columns else 0
    eval_cols = [
        "제약사명",
        "월시점 예상매출",
        "월시점 잔여예상",
        "월시점 달성률",
    ]
    if current_month and all(c in work.columns for c in eval_cols):
        eval_metrics = (
            work[work["기준월"].astype(str) == str(current_month)][eval_cols]
            .drop_duplicates("제약사명")
            .rename(
                columns={
                    "월시점 예상매출": "당월 예상매출",
                    "월시점 잔여예상": "당월 잔여예상",
                    "월시점 달성률": "당월 진척률",
                }
            )
        )
        out = out.merge(eval_metrics, on="제약사명", how="left", validate="one_to_one")
    for c in ["당월 예상매출", "당월 잔여예상", "당월 진척률"]:
        if c not in out.columns:
            out[c] = 0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)
    out["매출발생월수"] = out[completed_cols].ne(0).sum(axis=1) if completed_cols else 0
    recent3_cols = completed_cols[-3:] if completed_cols else []
    recent6_cols = completed_cols[-6:] if completed_cols else []
    out["최근3개월평균매출"] = out[recent3_cols].sum(axis=1) / len(recent3_cols) if recent3_cols else 0
    out["최근6개월평균매출"] = out[recent6_cols].sum(axis=1) / len(recent6_cols) if recent6_cols else 0
    out["최근3개월증감률"] = [
        _pct_change(r3, r6)
        for r3, r6 in zip(out["최근3개월평균매출"].tolist(), out["최근6개월평균매출"].tolist())
    ]
    out["추세판정"] = out.apply(_trend_judge_from_row, axis=1)
    out["기간구분"] = _display_period_label(policy.get("evaluation_mode"))
    out = out.sort_values(["총매출공급가액", "최근3개월평균매출", "제약사명"], ascending=[False, False, True]).reset_index(drop=True)
    top = int(params.get("top") or 0)
    if top > 0:
        out = out.head(top).copy()
    if "순번" in out.columns:
        out = out.drop(columns=["순번"])
    out.insert(0, "순번", range(1, len(out) + 1))
    summary_columns = _manufacturer_summary_public_columns(policy)
    label_map = _manufacturer_summary_eval_label_map(policy)
    for internal_col, public_col in label_map.items():
        if public_col != internal_col and internal_col in out.columns:
            out[public_col] = out[internal_col]
            out = out.drop(columns=[internal_col])
    out = _clean_public_columns(out, summary_columns, month_cols)
    out = _normalize_analytics_numeric_columns(out)
    out.attrs.update(getattr(detail, "attrs", {}) or {})
    out.attrs["months"] = months
    out.attrs["evaluation_mode"] = policy.get("evaluation_mode")
    out.attrs["evaluation_month"] = policy.get("evaluation_month")
    out.attrs["source_label"] = getattr(detail, "attrs", {}).get("source_label")
    out.attrs["display_period_label"] = _display_period_label(policy.get("evaluation_mode"))
    out.attrs["period_caption"] = _manufacturer_summary_period_caption(months, params, policy)
    t_calc = time.perf_counter()
    evaluation_month = str(policy.get("evaluation_month") or "")
    completed_groups = int(work[work["기준월"].astype(str) < evaluation_month]["제약사명"].nunique()) if evaluation_month else 0
    evaluation_groups = int(work[work["기준월"].astype(str) == evaluation_month]["제약사명"].nunique())
    result_groups = int(out["제약사명"].nunique())
    union_groups = int(work["제약사명"].nunique())
    dropped = max(union_groups - result_groups, 0)
    log.info(
        "[analytics.manufacturer_sales_trend.universe] completed_groups=%s evaluation_groups=%s union_groups=%s result_groups=%s dropped_groups=%s",
        completed_groups, evaluation_groups, union_groups, result_groups, dropped,
    )
    t_finish = time.perf_counter()
    log.info(
        "[analytics.manufacturer_sales_trend_summary.perf] raw_rows=%s month_rows=%s group_count=%s source=%.3fs group=%.3fs pivot=%.3fs calc=%.3fs finish=%.3fs total=%.3fs",
        len(detail), len(work), result_groups,
        t_source - t0, t_group - t_source, t_pivot - t_group, t_calc - t_pivot, t_finish - t_calc, t_finish - t0,
    )
    return out


def get_manufacturer_sales_trend_result(
    params: Optional[Dict[str, Any]] = None,
    raw_df: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    params = _apply_month_or_date_params(coalesce_params(params))
    df = get_manufacturer_sales_trend(params, raw_df=raw_df)
    rows = 0 if df is None else int(len(df))
    log.info("[analytics.manufacturer_sales_trend] rows=%s meta=%s", rows, _safe_analytics_log_meta(params))
    source_label = str(getattr(df, "attrs", {}).get("source_label") or _effective_source_label(_resolve_source_mode(params), df))
    query_summary = _fmt_analytics_query_summary(params, source_label)
    meta = _manufacturer_trend_meta(df, months=list(getattr(df, "attrs", {}).get("months") or []), policy=getattr(df, "attrs", {}) or {})
    meta.update({
        "analytics": True,
        "analysis_type": "manufacturer_sales_trend",
        "summary_type": "manufacturer_trend_detail",
        "source_label": source_label,
        "period_caption": getattr(df, "attrs", {}).get("period_caption") or _manufacturer_detail_period_caption(list(getattr(df, "attrs", {}).get("months") or []), getattr(df, "attrs", {}) or {}),
        "display_period_label": getattr(df, "attrs", {}).get("display_period_label") or _display_period_label(meta.get("evaluation_mode")),
        "query_summary": query_summary,
        "condition": query_summary,
        "summary_md": (
            f"제약사별 매출 추세 분석: 조회조건 {query_summary} / "
            f"제약사수 {_fmt_num_for_summary(meta.get('manufacturer_count'))} / "
            f"조회월수 {_fmt_num_for_summary(meta.get('month_count'))} / "
            f"총매출공급가액 {_fmt_num_for_summary(meta.get('sum_supply_amt'))} / "
            f"월평균매출 {_fmt_num_for_summary(meta.get('avg_month_sales_amt'))} / "
            f"추세판정 {_fmt_counts_for_summary(meta.get('trend_judge_counts') or {})} / "
            f"자료원 {source_label}"
        ),
    })
    payload = build_result_payload(
        table=TABLE,
        title="제약사별 매출 추세 분석",
        action="제약사별 매출 추세 분석",
        params=params,
        df=df,
        message=f"제약사별 매출 추세 분석 {rows:,}건",
    )
    payload.setdefault("meta", {})
    payload["meta"].update(meta)
    return payload


def get_manufacturer_sales_trend_summary_result(
    params: Optional[Dict[str, Any]] = None,
    raw_df: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    params = _apply_month_or_date_params(coalesce_params(params))
    df = get_manufacturer_sales_trend_summary(params, raw_df=raw_df)
    rows = 0 if df is None else int(len(df))
    log.info("[analytics.manufacturer_sales_trend_summary] rows=%s meta=%s", rows, _safe_analytics_log_meta(params))
    source_label = str(getattr(df, "attrs", {}).get("source_label") or _effective_source_label(_resolve_source_mode(params), df))
    query_summary = _fmt_analytics_query_summary(params, source_label)
    meta = _manufacturer_summary_meta(df, months=list(getattr(df, "attrs", {}).get("months") or []), policy=getattr(df, "attrs", {}) or {})
    meta.update({
        "analytics": True,
        "analysis_type": "manufacturer_sales_trend_summary",
        "summary_type": "manufacturer_trend_summary",
        "source_label": source_label,
        "period_caption": getattr(df, "attrs", {}).get("period_caption") or _manufacturer_summary_period_caption(list(getattr(df, "attrs", {}).get("months") or []), params, getattr(df, "attrs", {}) or {}),
        "display_period_label": getattr(df, "attrs", {}).get("display_period_label") or _display_period_label(meta.get("evaluation_mode")),
        "query_summary": query_summary,
        "condition": query_summary,
        "summary_md": (
            f"제약사별 매출 추세 분석 요약표: 조회조건 {query_summary} / "
            f"제약사수 {_fmt_num_for_summary(meta.get('manufacturer_count'))} / "
            f"완료월총매출 {_fmt_num_for_summary(meta.get('sum_completed_month_sales_amt'))} / "
            f"전체완료월평균매출 {_fmt_num_for_summary(meta.get('avg_completed_month_sales_amt'))} / "
            f"{str(meta.get('current_sales_label') or '당월 현재매출').replace(' ', '')} {_fmt_num_for_summary(meta.get('sum_current_month_sales_amt'))} / "
            f"매출없는제약사수 {_fmt_num_for_summary(meta.get('no_current_sales_group_count'))} / "
            f"추세판정 {_fmt_counts_for_summary(meta.get('trend_judge_counts') or {})} / "
            f"자료원 {source_label}"
        ),
    })
    payload = build_result_payload(
        table=TABLE,
        title="제약사별 매출 추세 분석 요약표",
        action="제약사별 매출 추세 분석 요약표",
        params=params,
        df=df,
        message=f"제약사별 매출 추세 분석 요약표 {rows:,}건",
    )
    payload.setdefault("meta", {})
    payload["meta"].update(meta)
    return payload

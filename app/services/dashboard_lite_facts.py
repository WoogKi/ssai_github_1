# app/services/dashboard_lite_facts.py
# -*- coding: utf-8 -*-
"""Dashboard Lite v0.1 deterministic facts.

The dashboard must explain pre-computed facts, not ask the LLM or infer totals
from a rendered sample.  This module accepts existing analytics payloads so
tests can run without a live ERP DB, and only calls production services when a
payload is not supplied by the UI.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime
import logging
import re
import time
from typing import Any, Mapping

import pandas as pd

from app.db.mssql_client import (
    DashboardQueryMeasurement,
    dashboard_measurement_phase,
    dashboard_query_measurement,
    get_active_dashboard_query_measurement,
)
from app.services.product_supplier_scope_service import apply_product_supplier_scope, supplier_scope_filter_active


FACTS_KIND = "SIMS_DASHBOARD_FACTS_V01"
STOCK_READY_THRESHOLD_PCT = 98.0
MAX_DASHBOARD_MONTHS = 13

log = logging.getLogger("ssai.sims.dashboard_lite")


def _normalize_yyyymm(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 6:
        return ""
    out = digits[:6]
    month = int(out[4:6] or 0)
    if month < 1 or month > 12:
        return ""
    return out


def _add_months(yyyymm: str, offset: int) -> str:
    y = int(yyyymm[:4])
    m = int(yyyymm[4:6]) + int(offset)
    while m <= 0:
        y -= 1
        m += 12
    while m > 12:
        y += 1
        m -= 12
    return f"{y:04d}{m:02d}"


def _last_day_yyyymm(yyyymm: str) -> str:
    y = int(yyyymm[:4])
    m = int(yyyymm[4:6])
    return f"{yyyymm}{monthrange(y, m)[1]:02d}"


def _dashboard_month_count(month_from: str, month_to: str) -> int:
    y1, m1 = int(month_from[:4]), int(month_from[4:6])
    y2, m2 = int(month_to[:4]), int(month_to[4:6])
    return (y2 - y1) * 12 + (m2 - m1) + 1


def default_dashboard_lite_scope(today: date | None = None, evaluation_month: Any = None) -> dict[str, str]:
    """Return six completed display months and a separate evaluation month."""
    today = today or date.today()
    eval_month = _normalize_yyyymm(evaluation_month) or f"{today.year:04d}{today.month:02d}"
    month_from = _add_months(eval_month, -6)
    month_to = _add_months(eval_month, -1)
    return {
        "month_from": month_from,
        "month_to": month_to,
        "evaluation_month": eval_month,
        "date_from": f"{month_from}01",
        "date_to": _last_day_yyyymm(month_to),
        "policy_date": today.strftime("%Y%m%d"),
        "source_mode": "monthly_book",
        "stock_mode": "real",
        "io_gu_list": [],
        "major_purchase_vendor_days": 90,
        "risk_analysis_days": 90,
        "overstock_inactive_days": 90,
        "readiness_warning_pct": 98.0,
        "risk_quick_view_count": 30,
        "amount_display_unit": "auto",
    }


def normalize_dashboard_lite_params(
    params: Mapping[str, Any] | None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Normalize and validate Dashboard Lite scope before any DB call."""
    today = today or date.today()
    raw = dict(params or {})
    evaluation_month = _normalize_yyyymm(raw.get("evaluation_month")) or _normalize_yyyymm(raw.get("month_to"))
    defaults = default_dashboard_lite_scope(today=today, evaluation_month=evaluation_month)

    month_from = _normalize_yyyymm(raw.get("month_from"))
    month_to = _normalize_yyyymm(raw.get("month_to"))
    if not month_from or not month_to:
        raise ValueError("Dashboard Lite 조회에는 시작월과 종료월이 필요합니다.")
    if month_from > month_to:
        raise ValueError("Dashboard Lite 시작월이 종료월보다 늦습니다.")

    month_count = _dashboard_month_count(month_from, month_to)
    if month_count <= 0:
        raise ValueError("Dashboard Lite 조회 범위가 비어 있습니다.")
    if month_count > MAX_DASHBOARD_MONTHS:
        raise ValueError("Dashboard Lite 조회 범위는 최대 13개월입니다.")

    out = dict(raw)
    out["month_from"] = month_from
    out["month_to"] = month_to
    out["evaluation_month"] = evaluation_month or month_to
    out["date_from"] = re.sub(r"\D", "", str(out.get("date_from") or ""))[:8] or f"{month_from}01"
    out["date_to"] = re.sub(r"\D", "", str(out.get("date_to") or ""))[:8] or (
        today.strftime("%Y%m%d") if month_to == f"{today.year:04d}{today.month:02d}" else _last_day_yyyymm(month_to)
    )
    out["policy_date"] = re.sub(r"\D", "", str(out.get("policy_date") or ""))[:8] or defaults["policy_date"]
    out["source_mode"] = str(out.get("source_mode") or defaults["source_mode"]).strip()
    out["stock_mode"] = str(out.get("stock_mode") or defaults["stock_mode"]).strip()
    if out["stock_mode"] not in {"real", "book"}:
        raise ValueError("재고기준은 실재고 또는 장부재고여야 합니다.")
    for key in (
        "stock_cd_list",
        "stock_name_list",
        "vendor_group_list",
        "vendor_kind_list",
        "product_group_list",
        "product_di_list",
        "product_class_list",
        "io_gu_list",
        "exclude_product_group_list",
        "exclude_product_group_nm_list",
        "exclude_product_di_list",
        "exclude_product_di_nm_list",
        "exclude_product_class_list",
        "exclude_product_class_nm_list",
    ):
        out[key] = _clean_list_param(out.get(key))
    out["io_gu_list"] = _clean_list_param(out.get("io_gu_list"))
    for key in ("major_purchase_vendor_days", "risk_analysis_days", "overstock_inactive_days", "risk_quick_view_count"):
        try:
            value = int(out.get(key, defaults[key]))
        except (TypeError, ValueError):
            raise ValueError(f"{key}는 1 이상의 정수여야 합니다.")
        if value < 1:
            raise ValueError(f"{key}는 1 이상의 정수여야 합니다.")
        out[key] = value
    try:
        readiness = float(out.get("readiness_warning_pct", defaults["readiness_warning_pct"]))
    except (TypeError, ValueError):
        raise ValueError("준비율 경고기준은 0 초과 100 이하의 숫자여야 합니다.")
    if not 0 < readiness <= 100:
        raise ValueError("준비율 경고기준은 0 초과 100 이하의 숫자여야 합니다.")
    out["readiness_warning_pct"] = readiness
    out["amount_display_unit"] = str(out.get("amount_display_unit") or "auto").strip().lower()
    if out["amount_display_unit"] not in {"auto", "won", "thousand", "million"}:
        raise ValueError("금액 표시단위가 올바르지 않습니다.")
    if out["vendor_group_list"] or out["vendor_kind_list"]:
        # Monthly stock aggregates do not carry the sales customer master;
        # use the established detail source so the customer code pairs are exact.
        out["source_mode"] = "detail"
    out["dashboard_product_group_list"] = list(out["product_group_list"])
    out["dashboard_product_di_list"] = list(out["product_di_list"])
    out["dashboard_product_class_list"] = list(out["product_class_list"])
    return apply_product_supplier_scope(out)


def _dashboard_internal_source_params(params: Mapping[str, Any], *, today: date) -> dict[str, Any]:
    """Load one shared source for legacy facts and demand-surge history."""
    display_from = str(params.get("month_from") or "")
    display_to = str(params.get("month_to") or "")
    evaluation_month = str(params.get("evaluation_month") or "")
    trend_month_from = _add_months(display_from, -3)
    # Keep the existing support range intact for legacy facts.  The expanded
    # history is used only for the demand-surge detail classification.
    history_month_from = _add_months(evaluation_month, -13)
    source_month_from = min(trend_month_from, history_month_from)
    today_ym = f"{today.year:04d}{today.month:02d}"
    source = apply_product_supplier_scope(params)
    source.update(
        {
            "month_from": source_month_from,
            "month_to": evaluation_month,
            "date_from": f"{source_month_from}01",
            "date_to": today.strftime("%Y%m%d") if evaluation_month == today_ym else _last_day_yyyymm(evaluation_month),
            "dashboard_lite_display_month_from": display_from,
            "dashboard_lite_display_month_to": display_to,
            "dashboard_lite_trend_month_from": trend_month_from,
            "dashboard_lite_history_month_from": history_month_from,
            # Dashboard uses the explicit Gcode:Tcode keys below.  Do not let
            # legacy single-code filters reinterpret Tax classification as Gu.
            "product_di_list": [],
            "product_class_list": [],
            # Supplier scope is temporary and is attached once to all shared sources.
        }
    )
    return source


def _dashboard_inbound_cutoff_date(params: Mapping[str, Any], *, today: date) -> str:
    """Use the evaluation-month policy date, never the completed-display month."""
    evaluation_month = _normalize_yyyymm(params.get("evaluation_month")) or f"{today.year:04d}{today.month:02d}"
    today_ym = f"{today.year:04d}{today.month:02d}"
    if evaluation_month < today_ym:
        return _last_day_yyyymm(evaluation_month)
    policy_text = re.sub(r"\D", "", str(params.get("policy_date") or ""))[:8]
    try:
        policy_day = datetime.strptime(policy_text, "%Y%m%d").date()
    except ValueError:
        policy_day = today
    return min(policy_day, today).strftime("%Y%m%d")


def resolve_transaction_cycle_status(purchase_status: Any, sales_status: Any) -> str:
    """Resolve the combined status without mutating the individual cycle states."""
    legacy_statuses = {
        "normal": "success",
        "insufficient": "single_trade_day",
        "missing": "no_data",
        "insufficient_days": "single_trade_day",
        "source_required": "unavailable",
        "": "unavailable",
    }

    def _normalized(value: Any) -> str:
        status = str(value or "").strip().lower()
        return legacy_statuses.get(status, status or "unavailable")

    purchase = _normalized(purchase_status)
    sales = _normalized(sales_status)
    available = {"success", "single_trade_day"}
    unavailable = {"no_data", "unavailable"}
    if purchase == "error" and sales == "error":
        return "error"
    if "error" in {purchase, sales}:
        return "degraded"
    if purchase in available and sales in available:
        return "ready"
    if purchase in available or sales in available:
        return "partial"
    if purchase in unavailable and sales in unavailable:
        return "no_data"
    return "partial"


def transaction_cycle_phase_timing(
    *,
    source_started_at: float,
    source_finished_at: float,
    calculation_started_at: float,
    calculation_finished_at: float,
) -> dict[str, int]:
    """Keep source and local-cycle calculation timings explicitly non-overlapping."""
    source_elapsed_ms = max(0, int((source_finished_at - source_started_at) * 1000))
    calculation_elapsed_ms = max(0, int((calculation_finished_at - calculation_started_at) * 1000))
    return {
        "source_elapsed_ms": source_elapsed_ms,
        "calculation_elapsed_ms": calculation_elapsed_ms,
        "total_elapsed_ms": source_elapsed_ms + calculation_elapsed_ms,
    }


def _stock_timing_meta(stock_attrs: Mapping[str, Any] | None, *, fallback_total_ms: int) -> dict[str, int]:
    """Forward SQL, aggregation, and shortage-build timings without conflation."""
    attrs = dict(stock_attrs or {})
    return {
        "stock_sql_ms": int(attrs.get("stock_sql_ms") or 0),
        "stock_batch_count": int(attrs.get("stock_query_batches") or 0),
        "stock_aggregate_ms": int(attrs.get("stock_aggregate_ms") or 0),
        "stock_shortage_build_ms": int(attrs.get("stock_shortage_build_ms") or 0),
        "stock_shortage_total_ms": int(attrs.get("stock_shortage_total_ms") or fallback_total_ms or 0),
        "configured_batch_size": int(attrs.get("configured_batch_size") or 0),
        "effective_chunk_size": int(attrs.get("effective_chunk_size") or 0),
        "fixed_parameter_count": int(attrs.get("fixed_parameter_count") or 0),
        "stock_cd_parameter_count": int(attrs.get("stock_cd_parameter_count") or 0),
        "io_gu_parameter_count": int(attrs.get("io_gu_parameter_count") or 0),
        "total_parameter_count": int(attrs.get("total_parameter_count") or 0),
    }


def _dashboard_visible_sales_df(df: pd.DataFrame | None, params: Mapping[str, Any]) -> pd.DataFrame | None:
    """Keep display completed months and the evaluation month for non-trend facts."""
    if df is None or df.empty or "기준월" not in df.columns:
        return df
    month_from = str(params.get("month_from") or "")
    evaluation_month = str(params.get("evaluation_month") or "")
    out = df.copy()
    months = out["기준월"].astype(str).str.replace(r"\D", "", regex=True).str[:6]
    return out[(months >= month_from) & (months <= evaluation_month)].copy()


def _dashboard_sales_df_for_month_range(
    df: pd.DataFrame | None,
    *,
    month_from: str,
    month_to: str,
) -> pd.DataFrame | None:
    """Return a month slice without changing the already-filtered source."""
    if df is None or df.empty or "기준월" not in df.columns:
        return df
    out = df.copy()
    months = out["기준월"].astype(str).str.replace(r"\D", "", regex=True).str[:6]
    return out[(months >= month_from) & (months <= month_to)].copy()


def _dashboard_source_months(df: pd.DataFrame | None) -> pd.Series | None:
    """Normalize source months once for all Dashboard-only range slices."""
    if df is None or df.empty or "기준월" not in df.columns:
        return None
    return df["기준월"].astype(str).str.replace(r"\D", "", regex=True).str[:6]


def _dashboard_sales_io_scope_meta(params: Mapping[str, Any]) -> tuple[str, int]:
    raw = params.get("io_gu_list") if isinstance(params, Mapping) else None
    if "io_gu_list" not in params:
        return "legacy_broad_fallback", 0
    if isinstance(raw, str):
        count = int(bool(raw.strip()))
    elif isinstance(raw, (list, tuple, set)):
        count = sum(1 for value in raw if isinstance(value, str) and value.strip())
    else:
        count = 0
    return ("exact_selected", count) if count else ("explicit_all", 0)


def _build_demand_surge_history_by_product(
    df: pd.DataFrame | None,
    *,
    evaluation_month: str,
    history_month_from: str,
    source_months: pd.Series | None = None,
) -> dict[str, Any]:
    """Build product-month net outbound history from the already-loaded source."""
    required = {"기준월", "제품코드", "출고수량"}
    source_ready = isinstance(df, pd.DataFrame) and required.issubset(set(df.columns))
    history_month_to = _add_months(evaluation_month, -1)
    context: dict[str, Any] = {
        "source_ready": source_ready,
        "history_month_from": history_month_from,
        "history_month_to": history_month_to,
        "months": [],
        "by_product": {},
    }
    if not source_ready:
        return context

    months = []
    cursor = history_month_from
    while cursor <= history_month_to:
        months.append(cursor)
        cursor = _add_months(cursor, 1)
    context["months"] = months
    months_series = source_months if isinstance(source_months, pd.Series) else _dashboard_source_months(df)
    if months_series is None:
        return context
    history_mask = months_series.between(history_month_from, history_month_to)
    work = df.loc[history_mask, ["제품코드", "출고수량"]].copy()
    work["기준월"] = months_series.loc[history_mask].to_numpy()
    work["제품코드"] = work["제품코드"].fillna("").astype(str).str.strip()
    work["출고수량"] = pd.to_numeric(work["출고수량"], errors="coerce").fillna(0.0)
    work = work[
        work["제품코드"].ne("")
        & work["기준월"].between(history_month_from, history_month_to)
    ]
    if work.empty:
        return context

    grouped = work.groupby(["제품코드", "기준월"], as_index=False)["출고수량"].sum()
    for product_code, part in grouped.groupby("제품코드", sort=False):
        amounts = {str(row["기준월"]): float(row["출고수량"] or 0.0) for _, row in part.iterrows()}
        context["by_product"][str(product_code)] = amounts
    return context


def _apply_demand_surge_detail(
    rows: list[dict[str, Any]],
    *,
    history: Mapping[str, Any],
    evaluation_month: str,
) -> dict[str, Any]:
    """Attach mutually exclusive detail reasons to already-adjusted surge rows."""
    epsilon = 1e-9
    history_month_from = str(history.get("history_month_from") or "")
    history_month_to = str(history.get("history_month_to") or "")
    source_ready = bool(history.get("source_ready"))
    by_product = history.get("by_product") if isinstance(history.get("by_product"), Mapping) else {}
    recent_months = [_add_months(evaluation_month, offset) for offset in (-3, -2, -1)]
    seasonal_months = [_add_months(evaluation_month, offset) for offset in (-13, -12, -11)]
    seasonal_complete = bool(
        source_ready
        and history_month_from
        and history_month_to
        and history_month_from <= seasonal_months[0]
        and history_month_to >= seasonal_months[-1]
    )
    counts = {
        "forecast_exceeded_rows": 0,
        "unexpected_outbound_rows": 0,
        "forecast_omission_rows": 0,
        "seasonal_recurrence_candidate_rows": 0,
        "reactivated_after_3m_rows": 0,
        "new_outbound_candidate_rows": 0,
        "insufficient_history_rows": 0,
    }

    for row in rows:
        current = float(row.get("당월현재출고수량") or 0.0)
        forecast = float(row.get("당월기준예상출고수량") or 0.0)
        product_code = str(row.get("product_code") or "").strip()
        top_category = ""
        detail_category = ""
        reason = ""
        history_values: Mapping[str, Any] = by_product.get(product_code, {}) if product_code else {}
        history_available = bool(product_code and source_ready)
        recent_values = [float(history_values.get(month) or 0.0) for month in recent_months]
        seasonal_values = [float(history_values.get(month) or 0.0) for month in seasonal_months]
        prior_positive_months = [
            month for month, value in history_values.items()
            if str(month) < recent_months[0] and float(value or 0.0) > epsilon
        ]
        history_positive_months = [
            month for month, value in history_values.items()
            if float(value or 0.0) > epsilon
        ]
        recent_positive_count = sum(value > epsilon for value in recent_values)
        row.update(
            {
                "최근1개월순출고수량": recent_values[2],
                "최근2개월순출고수량": recent_values[1],
                "최근3개월순출고수량": recent_values[0],
                "최근3개월순출고합계": float(sum(recent_values)),
                "최근3개월양의출고발생월수": int(recent_positive_count),
                "최근3개월출고여부": bool(recent_positive_count > 0),
                "전년동월순출고수량": seasonal_values[1],
                "전년동월전1개월순출고수량": seasonal_values[0],
                "전년동월후1개월순출고수량": seasonal_values[2],
                "계절성기준출고여부": bool(any(value > epsilon for value in seasonal_values)) if seasonal_complete else False,
                "계절성기준자료완전": seasonal_complete,
                "지원기간과거양의출고여부": bool(prior_positive_months),
                "지원기간과거양의출고월수": int(len(prior_positive_months)),
                "이력지원시작월": history_month_from,
                "이력지원종료월": history_month_to,
                "수요급증상위분류": "",
                "수요급증세부분류": "",
                "수요급증세부분류사유": "",
            }
        )
        if not bool(row.get("수요급증여부")):
            continue
        if forecast > epsilon and current > forecast + epsilon:
            top_category = "기존 예상 초과"
            detail_category = top_category
            reason = "당월 현재출고수량이 기준 예상출고수량 초과"
            counts["forecast_exceeded_rows"] += 1
        elif abs(forecast) <= epsilon and current > epsilon:
            top_category = "예상외 출고 발생"
            counts["unexpected_outbound_rows"] += 1
            if not history_available:
                detail_category = "분류자료부족"
                reason = "제품코드 또는 출고 이력 원천 없음"
                counts["insufficient_history_rows"] += 1
            elif recent_positive_count > 0:
                detail_category = "예상 누락"
                reason = "최근 3개월 완료월 출고 이력이 있으나 당월 기준예상은 0"
                counts["forecast_omission_rows"] += 1
            elif not seasonal_complete:
                detail_category = "분류자료부족"
                reason = "계절성 판단에 필요한 전년 동월 ±1개월 이력 부족"
                counts["insufficient_history_rows"] += 1
            elif any(value > epsilon for value in seasonal_values):
                detail_category = "계절성 재발생 후보"
                reason = "최근 3개월 무출고이며 전년 동월 ±1개월 양의 순출고 이력 존재"
                counts["seasonal_recurrence_candidate_rows"] += 1
            elif prior_positive_months:
                detail_category = "3개월 이상 재출고"
                reason = "최근 3개월 무출고 후 지원기간 과거 양의 순출고 이력 존재"
                counts["reactivated_after_3m_rows"] += 1
            elif not history_positive_months:
                detail_category = "신규 출고 후보"
                reason = "지원기간 완료월에 양의 순출고 이력 없음"
                counts["new_outbound_candidate_rows"] += 1
            else:
                detail_category = "분류자료부족"
                reason = "수요급증 이력 분류에 필요한 사실값 부족"
                counts["insufficient_history_rows"] += 1
        else:
            top_category = "예상외 출고 발생"
            counts["unexpected_outbound_rows"] += 1
            detail_category = "분류자료부족"
            reason = "수요급증 조건과 세부 분류 조건 불일치"
            counts["insufficient_history_rows"] += 1
        row["수요급증상위분류"] = top_category
        row["수요급증세부분류"] = detail_category
        row["수요급증세부분류사유"] = reason

    return {
        **counts,
        "total_rows": int(counts["forecast_exceeded_rows"] + counts["unexpected_outbound_rows"]),
        "history_month_from": history_month_from,
        "history_month_to": history_month_to,
        "recent_month_count": 3,
        "seasonality_rule": "전년 동월 ±1개월",
    }


def _clean_list_param(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw = [values]
    elif isinstance(values, (list, tuple, set)):
        raw = list(values)
    else:
        raw = [values]
    out: list[str] = []
    for value in raw:
        text = str(value or "").strip()
        if text and text != "전체" and text not in out:
            out.append(text)
    return out


def _clean_sorted(values: Any) -> list[str]:
    return sorted(_clean_list_param(values))


def _parse_code_pair(value: Any, default_gcode: str = "") -> tuple[str, str]:
    text = str(value or "").strip()
    if ":" in text:
        gcode, tcode = text.split(":", 1)
        return gcode.strip(), tcode.strip()
    return str(default_gcode or "").strip(), text


def _code_pair_items(values: Any, names: Any = None, default_gcode: str = "") -> list[dict[str, str]]:
    codes = _clean_list_param(values)
    name_list = _clean_list_param(names)
    out: list[dict[str, str]] = []
    for idx, raw in enumerate(codes):
        gcode, tcode = _parse_code_pair(raw, default_gcode)
        if not tcode:
            continue
        out.append(
            {
                "gcode": gcode,
                "tcode": tcode,
                "name": name_list[idx] if idx < len(name_list) else "",
            }
        )
    return out


def _first_text(row: pd.Series, columns: list[str], default: str = "") -> str:
    for col in columns:
        if col in row.index:
            text = str(row.get(col) or "").strip()
            if text:
                return text
    return default


def _dashboard_filter_mask(
    df: pd.DataFrame,
    *,
    label: str,
    gcode_columns: tuple[str, ...],
    code_columns: tuple[str, ...],
    code_values: list[str],
    default_gcode: str,
    name_columns: tuple[str, ...],
    name_values: list[str],
) -> tuple[pd.Series, dict[str, Any] | None]:
    """Return an exclusion mask using the exact ERP Gcode + Tcode pair."""
    mask = pd.Series(False, index=df.index)
    code_pairs = _code_pair_items(code_values, default_gcode=default_gcode)
    selected_code_pair_count = len(code_pairs)

    gcode_col = next((col for col in gcode_columns if col in df.columns), "")
    tcode_col = next((col for col in code_columns if col in df.columns), "")
    if code_pairs and gcode_col and tcode_col:
        gvalues = df[gcode_col].fillna("").astype(str).str.strip()
        tvalues = df[tcode_col].fillna("").astype(str).str.strip()
        for pair in code_pairs:
            mask = mask | ((gvalues == pair["gcode"]) & (tvalues == pair["tcode"]))
        return mask, {
            "label": label,
            "filter_basis": "code_pair",
            "missing_code_columns": [],
            "selected_code_pair_count": selected_code_pair_count,
            "filtered_rows": int(mask.sum()),
        }

    missing = []
    if code_pairs and not gcode_col:
        missing.append("gcode")
    if code_pairs and not tcode_col:
        missing.append("tcode")
    return mask, {
        "label": label,
        "filter_basis": "not_applied",
        "missing_code_columns": missing,
        "selected_code_pair_count": selected_code_pair_count,
        "filtered_rows": 0,
    }


def _filter_sales_source_for_dashboard(df: pd.DataFrame | None, params: Mapping[str, Any]) -> pd.DataFrame | None:
    """Apply current inclusion filters or the legacy exclusion compatibility path."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df
    inclusion_keys = (
        "product_group_list",
        "product_di_list",
        "product_class_list",
    )
    legacy_exclusion_keys = (
        "exclude_product_group_list",
        "exclude_product_di_list",
        "exclude_product_class_list",
    )
    use_inclusion = any(_clean_list_param(params.get(key)) for key in inclusion_keys)
    active_keys = inclusion_keys if use_inclusion else legacy_exclusion_keys
    if not any(
        _clean_list_param(params.get(key))
        for key in active_keys
    ):
        return df
    source_attrs = dict(getattr(df, "attrs", {}) or {})
    out = df
    inclusion_specs = [
        {
            "label": "제품그룹",
            "gcode_columns": ("제품그룹Gcode", "제품그룹G코드", "product_group_gcode", "group_gcode"),
            "code_columns": ("제품그룹코드", "상품그룹코드", "품목그룹코드", "product_group_code", "product_group_cd", "group_code"),
            "code_values": _clean_list_param(params.get("product_group_list" if use_inclusion else "exclude_product_group_list")),
            "default_gcode": "0013",
            "name_columns": ("제품그룹명", "상품그룹명", "품목그룹명", "product_group_name", "group_name"),
            "name_values": _clean_list_param(params.get("product_group_nm_list" if use_inclusion else "exclude_product_group_nm_list")),
        },
        {
            "label": "제품구분",
            "gcode_columns": ("제품구분Gcode", "제품구분G코드", "product_di_gcode", "product_type_gcode", "type_gcode"),
            "code_columns": ("제품구분코드", "상품구분코드", "품목구분코드", "product_di_code", "product_di_cd", "product_type_code", "type_code"),
            "code_values": _clean_list_param(params.get("product_di_list" if use_inclusion else "exclude_product_di_list")),
            "default_gcode": "0004",
            "name_columns": ("제품구분명", "상품구분명", "품목구분명", "product_di_name", "product_type_name", "type_name"),
            "name_values": _clean_list_param(params.get("product_di_nm_list" if use_inclusion else "exclude_product_di_nm_list")),
        },
        {
            "label": "제품분류",
            "gcode_columns": ("제품분류Gcode", "제품분류G코드", "product_class_gcode", "class_gcode"),
            "code_columns": ("제품분류코드", "상품분류코드", "품목분류코드", "product_class_code", "product_class_cd", "class_code"),
            "code_values": _clean_list_param(params.get("product_class_list" if use_inclusion else "exclude_product_class_list")),
            "default_gcode": "0031",
            "name_columns": ("제품분류명", "상품분류명", "품목분류명", "product_class_name", "class_name"),
            "name_values": _clean_list_param(params.get("product_class_nm_list" if use_inclusion else "exclude_product_class_nm_list")),
        },
    ]
    product_col = "제품코드" if "제품코드" in out.columns else ""
    dimension_columns = [product_col] if product_col else []
    for spec in inclusion_specs:
        for columns_key in ("gcode_columns", "code_columns"):
            column = next((candidate for candidate in spec[columns_key] if candidate in out.columns), "")
            if column and column not in dimension_columns:
                dimension_columns.append(column)
    # Product dimensions are repeated for every month.  Resolve the selected
    # product universe once, then reuse that set against the expanded source.
    filter_df = (
        out.loc[:, dimension_columns].drop_duplicates(subset=[product_col], keep="last")
        if product_col and dimension_columns
        else out
    )
    keep_mask = pd.Series(True, index=filter_df.index)
    exclude_mask = pd.Series(False, index=filter_df.index)
    diagnostics: list[dict[str, Any]] = []
    for spec in inclusion_specs:
        spec_mask, diag = _dashboard_filter_mask(filter_df, **spec)
        selected_code_values = _clean_list_param(spec.get("code_values"))
        if diag and selected_code_values:
            diagnostics.append(diag)
            if diag.get("filter_basis") == "code_pair":
                if use_inclusion:
                    keep_mask = keep_mask & spec_mask
                else:
                    exclude_mask = exclude_mask | spec_mask
            log.info(
                "[dashboard.filter] label=%s filter_basis=%s missing_code_columns=%s selected_code_pair_count=%s filtered_rows=%s",
                diag.get("label") or "",
                diag.get("filter_basis") or "",
                ",".join(diag.get("missing_code_columns") or []),
                int(diag.get("selected_code_pair_count") or 0),
                int(spec_mask.sum()) if hasattr(spec_mask, "sum") else 0,
            )
    if product_col:
        if use_inclusion and not bool(keep_mask.all()):
            selected_products = set(filter_df.loc[keep_mask, product_col].dropna().astype(str))
            out = out.loc[out[product_col].astype(str).isin(selected_products)].copy()
        elif not use_inclusion and bool(exclude_mask.any()):
            excluded_products = set(filter_df.loc[exclude_mask, product_col].dropna().astype(str))
            out = out.loc[~out[product_col].astype(str).isin(excluded_products)].copy()
    elif use_inclusion and not bool(keep_mask.all()):
        out = out.loc[keep_mask].copy()
    elif not use_inclusion and bool(exclude_mask.any()):
        out = out.loc[~exclude_mask].copy()
    out.attrs.update(source_attrs)
    out.attrs["dashboard_filter_diagnostics"] = diagnostics
    return out


def _filter_payload_df_for_dashboard(payload: Mapping[str, Any] | None, params: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        return payload
    df = _payload_df(payload)
    if df.empty:
        return payload
    filtered = _filter_sales_source_for_dashboard(df, params)
    if filtered is df:
        return payload
    out = dict(payload)
    if isinstance(payload.get("df"), pd.DataFrame):
        out["df"] = filtered
    elif isinstance(payload.get("df_display"), pd.DataFrame):
        out["df_display"] = filtered
    elif isinstance(payload.get("records"), list):
        out["records"] = filtered.to_dict(orient="records")
    if isinstance(out.get("meta"), Mapping):
        meta = dict(out.get("meta") or {})
        meta["row_count"] = len(filtered)
        out["meta"] = meta
    return out


def _dashboard_filter_facts(params: Mapping[str, Any]) -> dict[str, Any]:
    stock_codes = _clean_list_param(params.get("stock_cd_list"))
    stock_names = _clean_list_param(params.get("stock_name_list"))
    group_codes = _clean_list_param(params.get("product_group_list"))
    group_names = _clean_list_param(params.get("product_group_nm_list"))
    di_codes = _clean_list_param(params.get("product_di_list"))
    di_names = _clean_list_param(params.get("product_di_nm_list"))
    class_codes = _clean_list_param(params.get("product_class_list"))
    class_names = _clean_list_param(params.get("product_class_nm_list"))
    vendor_group_codes = _clean_list_param(params.get("vendor_group_list"))
    vendor_kind_codes = _clean_list_param(params.get("vendor_kind_list"))
    supplier_scope = apply_product_supplier_scope(params)
    legacy_group_codes = _clean_list_param(params.get("exclude_product_group_list"))
    legacy_group_names = _clean_list_param(params.get("exclude_product_group_nm_list"))
    legacy_di_codes = _clean_list_param(params.get("exclude_product_di_list"))
    legacy_di_names = _clean_list_param(params.get("exclude_product_di_nm_list"))
    legacy_class_codes = _clean_list_param(params.get("exclude_product_class_list"))
    legacy_class_names = _clean_list_param(params.get("exclude_product_class_nm_list"))
    included_stock_locations = []
    for idx, code in enumerate(stock_codes):
        included_stock_locations.append(
            {
                "code": code,
                "name": stock_names[idx] if idx < len(stock_names) else "",
            }
        )
    return {
        "included_stock_locations": included_stock_locations,
        "included_product_groups": _code_pair_items(group_codes, group_names, "0013"),
        "included_product_types": _code_pair_items(di_codes, di_names, "0004"),
        "included_product_classes": _code_pair_items(class_codes, class_names, "0031"),
        "excluded_product_groups": _code_pair_items(legacy_group_codes, legacy_group_names, "0013"),
        "excluded_product_types": _code_pair_items(legacy_di_codes, legacy_di_names, "0004"),
        "excluded_product_classes": _code_pair_items(legacy_class_codes, legacy_class_names, "0031"),
        "included_vendor_groups": _code_pair_items(vendor_group_codes, [], "0019"),
        "included_vendor_types": _code_pair_items(vendor_kind_codes, [], "0009"),
        "product_supplier_scope_mode": supplier_scope["product_supplier_scope_mode"],
        "manufacturer_codes": supplier_scope["manufacturer_codes"],
        # Legacy provenance key is retained for pre-existing compact clients.
        "manufacturer_test_codes": supplier_scope["manufacturer_codes"],
        "manufacturer_manager_codes": supplier_scope["manufacturer_manager_codes"],
        "order_vendor_codes": supplier_scope["order_vendor_codes"],
        "purchase_manager_codes": supplier_scope["purchase_manager_codes"],
        "io_gu_tcodes": _clean_list_param(params.get("io_gu_list")),
        "stock_mode": str(params.get("stock_mode") or "real"),
        "amount_display_unit": str(params.get("amount_display_unit") or "auto"),
        "thresholds": {
            "major_purchase_vendor_days": params.get("major_purchase_vendor_days"),
            "risk_analysis_days": params.get("risk_analysis_days"),
            "overstock_inactive_days": params.get("overstock_inactive_days"),
            "readiness_warning_pct": params.get("readiness_warning_pct"),
            "risk_quick_view_count": params.get("risk_quick_view_count"),
        },
    }


def _payload_df(payload: Mapping[str, Any] | None) -> pd.DataFrame:
    if not isinstance(payload, Mapping):
        return pd.DataFrame()
    for key in ("df", "df_display"):
        obj = payload.get(key)
        if isinstance(obj, pd.DataFrame):
            return obj.copy()
    records = payload.get("records")
    if isinstance(records, list):
        try:
            return pd.DataFrame(records)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


_DASHBOARD_PRODUCT_CODE_PAIR_COLUMNS = (
    "\uc81c\ud488\uadf8\ub8f9Gcode",
    "\uc81c\ud488\uadf8\ub8f9\ucf54\ub4dc",
    "\uc81c\ud488\uadf8\ub8f9\uba85",
    "\uc81c\ud488\uad6c\ubd84Gcode",
    "\uc81c\ud488\uad6c\ubd84\ucf54\ub4dc",
    "\uc81c\ud488\uad6c\ubd84\uba85",
    "\uc81c\ud488\ubd84\ub958Gcode",
    "\uc81c\ud488\ubd84\ub958\ucf54\ub4dc",
    "\uc81c\ud488\ubd84\ub958\uba85",
)


def _attach_dashboard_product_code_pairs(
    payload: Mapping[str, Any] | None,
    sales_source: pd.DataFrame | None,
) -> Mapping[str, Any] | None:
    """Enrich a Dashboard stock payload with product-master code pairs from its shared sales source."""
    if not isinstance(payload, Mapping) or not isinstance(sales_source, pd.DataFrame):
        return payload
    product_col = "\uc81c\ud488\ucf54\ub4dc"
    if product_col not in sales_source.columns:
        return payload
    available = [col for col in _DASHBOARD_PRODUCT_CODE_PAIR_COLUMNS if col in sales_source.columns]
    if not available:
        return payload
    source_pairs = sales_source.loc[:, [product_col, *available]].drop_duplicates(subset=[product_col], keep="last")
    if source_pairs.empty:
        return payload

    def _enrich(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or product_col not in df.columns:
            return df
        attrs = dict(getattr(df, "attrs", {}) or {})
        out = df.copy()
        lookup = source_pairs.set_index(product_col)
        for column in available:
            mapped = out[product_col].map(lookup[column])
            if column not in out.columns:
                out[column] = mapped
            else:
                existing = out[column]
                missing = existing.isna() | existing.astype(str).str.strip().eq("")
                out.loc[missing, column] = mapped.loc[missing]
        out.attrs.update(attrs)
        return out

    out = dict(payload)
    for key in ("df", "df_display"):
        if isinstance(payload.get(key), pd.DataFrame):
            out[key] = _enrich(payload[key])
    if isinstance(payload.get("records"), list):
        records_df = _enrich(pd.DataFrame(payload["records"]))
        out["records"] = records_df.to_dict(orient="records")
    return out


def _payload_meta(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if isinstance(payload, Mapping) and isinstance(payload.get("meta"), Mapping):
        return dict(payload.get("meta") or {})
    return {}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if pd.isna(value):
            return default
    except Exception:
        pass
    try:
        return float(str(value).replace(",", "").strip())
    except Exception:
        return default


def _sum_col(df: pd.DataFrame, col: str) -> float:
    if not isinstance(df, pd.DataFrame) or col not in df.columns:
        return 0.0
    return float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return float(numerator) / float(denominator) if float(denominator or 0) else default


def _dashboard_time_progress(
    evaluation_month: Any,
    *,
    policy_date: Any = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Return the calendar progress used only to interpret the evaluation month."""
    today = today or date.today()
    yyyymm = _normalize_yyyymm(evaluation_month)
    if not yyyymm:
        return {"pct": None, "elapsed_days": None, "total_days": None, "status": "자료부족"}

    today_ym = today.strftime("%Y%m")
    if yyyymm < today_ym:
        return {"pct": 100.0, "elapsed_days": monthrange(int(yyyymm[:4]), int(yyyymm[4:6]))[1], "total_days": monthrange(int(yyyymm[:4]), int(yyyymm[4:6]))[1], "status": "완료월"}
    if yyyymm > today_ym:
        return {"pct": 0.0, "elapsed_days": 0, "total_days": monthrange(int(yyyymm[:4]), int(yyyymm[4:6]))[1], "status": "미래월"}

    total_days = monthrange(today.year, today.month)[1]
    policy_text = re.sub(r"\D", "", str(policy_date or ""))[:8]
    try:
        policy_day = datetime.strptime(policy_text, "%Y%m%d").date()
    except ValueError:
        policy_day = today
    if policy_day.strftime("%Y%m") != yyyymm:
        policy_day = today
    elapsed_days = max(0, min(int(policy_day.day), total_days))
    return {
        "pct": float(elapsed_days / total_days * 100.0),
        "elapsed_days": elapsed_days,
        "total_days": total_days,
        "status": "진행중",
    }


def _fact(
    label: str,
    value: Any,
    *,
    unit: str = "",
    aggregation: str = "",
    grain: str = "",
    time_basis: str = "",
    source_columns: list[str] | None = None,
    partial_period: bool = False,
    comparable_with: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "label": label,
        "value": value,
        "unit": unit,
        "aggregation": aggregation,
        "grain": grain,
        "time_basis": time_basis,
        "source_columns": source_columns or [],
        "partial_period": bool(partial_period),
        "comparable_with": comparable_with or [],
    }


def _month_sales_columns(df: pd.DataFrame) -> list[str]:
    cols: list[str] = []
    for col in df.columns if isinstance(df, pd.DataFrame) else []:
        s = str(col or "").strip()
        if re.match(r"^\d{4}-\d{2}\s+매출$", s) or re.match(r"^\d{6}\s+매출$", s):
            cols.append(s)
    return sorted(cols)


def _monthly_sales_actuals_from_source(df: pd.DataFrame | None) -> list[dict[str, Any]]:
    """Aggregate the already-loaded sales source for chart forecast history."""
    if not isinstance(df, pd.DataFrame) or df.empty or "기준월" not in df.columns:
        return []
    amount_col = next((col for col in ("매출합계", "총매출액", "합계금액") if col in df.columns), "")
    if not amount_col:
        return []
    work = df.loc[:, ["기준월", amount_col]].copy()
    work["기준월"] = work["기준월"].map(_normalize_yyyymm)
    work[amount_col] = pd.to_numeric(work[amount_col], errors="coerce").fillna(0)
    grouped = work[work["기준월"].ne("")].groupby("기준월", as_index=False)[amount_col].sum()
    return [
        {
            "period": f"{str(row['기준월'])[:4]}-{str(row['기준월'])[4:6]}",
            "period_sort": str(row["기준월"]),
            "value": float(row[amount_col]),
        }
        for _, row in grouped.sort_values("기준월", kind="stable").iterrows()
    ]


def _chart_period(value: Any) -> tuple[str, str]:
    """Return the display month and its stable YYYYMM ordering value."""
    yyyymm = _normalize_yyyymm(value)
    if not yyyymm:
        return str(value or ""), ""
    return f"{yyyymm[:4]}-{yyyymm[4:6]}", yyyymm


def _completed_month_pre_forecasts(
    monthly_actuals: list[dict[str, Any]],
    *,
    history_actuals: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Calculate a target month's forecast from preceding completed months only."""
    if not monthly_actuals:
        return []

    # Reuse the production forecast grade/base/rate formula without any service
    # call. A target month's actual is deliberately excluded from its input row.
    from app.services.analytics_sales_trend_service import (
        _forecast_projection_from_row,
        _pct_change,
        _trend_judge,
    )

    forecasts: list[dict[str, Any]] = []
    history = [
        float(item.get("value") or 0)
        for item in (history_actuals or [])
        if str(item.get("period_sort") or "") < str(monthly_actuals[0].get("period_sort") or "")
    ]
    for target in monthly_actuals:
        active_months = sum(1 for value in history if value != 0)
        if active_months > 1:
            completed_total = float(sum(history))
            completed_count = len(history)
            completed_avg = _safe_div(completed_total, completed_count)
            recent3 = float(sum(history[-3:]) / min(3, completed_count)) if completed_count else 0.0
            recent6 = float(sum(history[-6:]) / min(6, completed_count)) if completed_count else 0.0
            growth_pct = float(_pct_change(recent3, recent6))
            trend = _trend_judge(completed_total, recent3, recent6, any(value < 0 for value in history))
            basis, applied_rate, projected = _forecast_projection_from_row(
                pd.Series(
                    {
                        "완료월총매출": completed_total,
                        "완료월수": completed_count,
                        "완료월평균매출": completed_avg,
                        "월평균매출": completed_avg,
                        "최근3개월평균매출": recent3,
                        "최근6개월평균매출": recent6,
                        "최근3개월증감률": growth_pct,
                        "매출발생월수": active_months,
                        "추세판정": trend,
                    }
                )
            )
            forecasts.append(
                {
                    "period": target["period"],
                    "period_sort": target["period_sort"],
                    "value": float(projected),
                    "kind": "완료월 사전예상",
                    "partial_period": False,
                    "forecast_basis": basis,
                    "forecast_status": "사전예상",
                    "applied_growth_pct": float(applied_rate),
                }
            )
        history.append(float(target["value"]))
    return forecasts


def _text_series(df: pd.DataFrame, col: str, default: str = "미지정") -> pd.Series:
    if col not in df.columns:
        return pd.Series([default] * len(df), index=df.index)
    out = df[col].fillna("").astype(str).str.strip()
    return out.where(out.ne(""), default)


STOCK_RISK_STATUS_ORDER = ("긴급 부족", "부족 주의", "적정", "판정 제외")


def _stock_risk_item_identity(work: pd.DataFrame) -> pd.Series:
    """Return the stable item identity used only for stock-risk summary counts."""
    product_codes = work.get("product_code", pd.Series("", index=work.index, dtype="object"))
    product_names = work.get("product_name", pd.Series("", index=work.index, dtype="object"))
    product_codes = product_codes.fillna("").astype(str).str.strip()
    product_names = product_names.fillna("").astype(str).str.strip()

    identities: list[str] = []
    for row_index, product_code, product_name in zip(work.index, product_codes, product_names):
        if product_code:
            identities.append(f"code:{product_code}")
        elif product_name:
            identities.append(f"name:{product_name}")
        else:
            identities.append(f"row:{row_index}")
    return pd.Series(identities, index=work.index, dtype="object")


def _apply_current_month_demand_surge(
    rows: list[dict[str, Any]],
    *,
    evaluation_month: str,
    policy_date: str,
) -> dict[str, Any]:
    """Add risk-only demand-surge fields without changing source shortage facts."""
    policy_digits = re.sub(r"\D", "", str(policy_date or ""))[:8]
    current_month = bool(policy_digits and evaluation_month == policy_digits[:6])
    elapsed_days = 0
    total_days = 0
    remaining_days = 0
    if current_month:
        year, month = int(policy_digits[:4]), int(policy_digits[4:6])
        total_days = monthrange(year, month)[1]
        elapsed_days = min(max(int(policy_digits[6:8]), 1), total_days)
        remaining_days = max(total_days - elapsed_days, 0)

    for row in rows:
        current_shipment = float(row.get("당월현재출고수량") or 0)
        base_forecast = float(row.get("당월기준예상출고수량") or 0)
        base_remaining = float(row.get("remaining_expected_demand_qty") or 0)
        stock = float(row.get("current_stock_qty") or 0)
        unit_price = float(row.get("stock_valuation_unit_price") or 0)
        demand_surge = bool(current_month and current_shipment > base_forecast)
        pace_month_end = (current_shipment / elapsed_days * total_days) if demand_surge else base_forecast
        adjusted_forecast = max(base_forecast, pace_month_end) if demand_surge else base_forecast
        adjusted_remaining = max(adjusted_forecast - current_shipment, 0.0) if demand_surge else base_remaining
        adjusted_shortage_qty = max(adjusted_remaining - stock, 0.0)
        adjusted_shortage_amt = adjusted_shortage_qty * unit_price
        adjusted_readiness = (
            min(max(stock, 0.0), adjusted_remaining) / adjusted_remaining * 100.0
            if adjusted_remaining > 0
            else float("nan")
        )
        row.update(
            {
                "수요급증여부": demand_surge,
                "수요급증사유": "당월 현재출고수량이 기준 예상출고수량 초과" if demand_surge else "",
                "평가월경과일수": elapsed_days,
                "평가월총일수": total_days,
                "평가월잔여일수": remaining_days,
                "진행속도기준월말예상출고수량": pace_month_end if demand_surge else None,
                "위험보정예상출고수량": adjusted_forecast,
                "위험보정잔여예상수요": adjusted_remaining,
                "위험보정재고준비율": adjusted_readiness,
                "위험보정부족예상수량": adjusted_shortage_qty,
                "위험보정부족예상금액": adjusted_shortage_amt,
                "위험보정기준": "진행속도 보정" if demand_surge else ("현재월 아님" if not current_month else "기존 예상"),
            }
        )
    return {
        "current_month": current_month,
        "evaluation_elapsed_days": elapsed_days,
        "evaluation_total_days": total_days,
        "evaluation_remaining_days": remaining_days,
    }


def _classify_stock_risk_rows(
    rows: list[dict[str, Any]],
    *,
    readiness_warning_pct: float,
) -> list[dict[str, Any]]:
    """Add v0.2 stock-risk facts without changing the existing shortage facts."""
    started = time.perf_counter()
    if not rows:
        summary = [
            {
                "재고위험상태": status,
                "품목수": 0,
                "부족예상금액": 0.0,
                "과잉후보금액": 0.0,
                "현재재고금액": 0.0,
            }
            for status in STOCK_RISK_STATUS_ORDER
        ]
        log.info(
            "[dashboard.stock_risk] total_rows=0 emergency_rows=0 warning_rows=0 normal_rows=0 excluded_rows=0 overstock_candidate_rows=0 emergency_amount=0 warning_shortage_amount=0 overstock_candidate_amount=0 demand_surge_rows=0 demand_surge_emergency_rows=0 demand_surge_warning_rows=0 demand_surge_normal_rows=0 adjusted_remaining_demand_qty=0 adjusted_shortage_qty=0 adjusted_shortage_amount=0 evaluation_elapsed_days=0 evaluation_total_days=0 readiness_warning_pct=%s elapsed_ms=%s",
            float(readiness_warning_pct),
            int((time.perf_counter() - started) * 1000),
        )
        return summary

    work = pd.DataFrame(rows)
    numeric_cols = [
        "current_stock_qty",
        "current_stock_amt",
        "remaining_expected_demand_qty",
        "shortage_qty",
        "shortage_amt",
        "stock_readiness_pct",
        "stock_valuation_unit_price",
        "3개월필요수량",
        "위험보정잔여예상수요",
        "위험보정재고준비율",
        "위험보정부족예상수량",
        "위험보정부족예상금액",
    ]
    numeric = pd.DataFrame(
        {
            col: pd.to_numeric(
                work.get(col, pd.Series(float("nan"), index=work.index, dtype="float64")),
                errors="coerce",
            )
            for col in numeric_cols
        },
        index=work.index,
    )
    required_present = work.get("_stock_risk_required_values_present", pd.Series(True, index=work.index))
    required_numeric_cols = [
        col
        for col in numeric_cols
        if col not in {"3개월필요수량", "위험보정잔여예상수요", "위험보정재고준비율", "위험보정부족예상수량", "위험보정부족예상금액"}
    ]
    required_present = required_present.fillna(False).astype(bool) & numeric[required_numeric_cols].notna().all(axis=1)
    demand = numeric["위험보정잔여예상수요"].where(
        numeric["위험보정잔여예상수요"].notna(),
        numeric["remaining_expected_demand_qty"],
    )
    stock = numeric["current_stock_qty"]
    demand_surge = work.get("수요급증여부", pd.Series(False, index=work.index)).fillna(False).astype(bool)
    coverage = numeric["위험보정재고준비율"].where(
        numeric["위험보정재고준비율"].notna(),
        (stock.clip(lower=0) / demand * 100.0).where(demand > 0),
    )
    excess_qty = (stock - demand).clip(lower=0).where(demand > 0, 0.0)
    excess_amt = excess_qty * numeric["stock_valuation_unit_price"]

    status = pd.Series("판정 제외", index=work.index, dtype="object")
    reason = pd.Series("필수값 누락", index=work.index, dtype="object")
    no_demand = required_present & demand.le(0)
    status.loc[no_demand] = "판정 제외"
    reason.loc[no_demand] = "수요없음"

    eligible = required_present & demand.gt(0)
    emergency = eligible & stock.lt(demand / 2.0)
    status.loc[emergency] = "긴급 부족"
    emergency_reasons = pd.Series("", index=work.index, dtype="object")
    emergency_reasons.loc[emergency & stock.le(0)] = "재고없음"
    emergency_reasons.loc[emergency & stock.gt(0) & demand_surge] = "수요급증 후 잔여수요 절반 미만"
    emergency_reasons.loc[emergency & emergency_reasons.eq("")] = "잔여수요 절반 미만"
    reason.loc[emergency] = emergency_reasons.loc[emergency]

    warning = eligible & ~emergency & coverage.lt(float(readiness_warning_pct))
    status.loc[warning] = "부족 주의"
    reason.loc[warning] = "준비율 경고기준 미만"
    reason.loc[warning & demand_surge] = "수요급증 후 준비율 경고기준 미만"

    normal = eligible & ~emergency & ~warning
    status.loc[normal] = "적정"
    reason.loc[normal] = "준비율 경고기준 이상"

    three_month_required_qty = numeric["3개월필요수량"].fillna(0.0)
    overstock_candidate = normal & ~demand_surge & three_month_required_qty.gt(0) & stock.gt(three_month_required_qty)
    overstock_candidate_qty = (stock - three_month_required_qty).clip(lower=0).where(overstock_candidate, 0.0)
    overstock_candidate_amt = overstock_candidate_qty * numeric["stock_valuation_unit_price"]

    work["재고위험상태"] = status
    work["재고위험사유"] = reason
    work["재고커버리지율"] = coverage
    work["과잉후보여부"] = overstock_candidate.fillna(False).astype(bool)
    work["과잉후보수량"] = overstock_candidate_qty.fillna(0.0)
    work["과잉후보금액"] = overstock_candidate_amt.fillna(0.0)
    work["과잉후보사유"] = pd.Series("", index=work.index, dtype="object")
    work.loc[overstock_candidate, "과잉후보사유"] = "현재재고가 3개월 필요수량 초과"
    rows[:] = work.drop(columns=["_stock_risk_required_values_present"], errors="ignore").to_dict("records")

    item_identity = _stock_risk_item_identity(work)
    summary: list[dict[str, Any]] = []
    for risk_status in STOCK_RISK_STATUS_ORDER:
        subset = work.loc[work["재고위험상태"].eq(risk_status)]
        item_count = int(item_identity.loc[subset.index].nunique())
        adjusted_shortage_amt = pd.to_numeric(
            subset.get("위험보정부족예상금액", subset.get("shortage_amt", pd.Series(0, index=subset.index))),
            errors="coerce",
        ).fillna(0)
        summary.append(
            {
                "재고위험상태": risk_status,
                "품목수": item_count,
                "부족예상금액": float(adjusted_shortage_amt.sum()),
                "과잉후보금액": float(pd.to_numeric(subset.get("과잉후보금액"), errors="coerce").fillna(0).sum()),
                "현재재고금액": float(pd.to_numeric(subset.get("current_stock_amt"), errors="coerce").fillna(0).sum()),
            }
        )

    for adjusted_col in (
        "위험보정잔여예상수요",
        "위험보정부족예상수량",
        "위험보정부족예상금액",
    ):
        if adjusted_col not in work.columns:
            work[adjusted_col] = 0.0

    log.info(
        "[dashboard.stock_risk] total_rows=%s emergency_rows=%s warning_rows=%s normal_rows=%s excluded_rows=%s overstock_candidate_rows=%s emergency_amount=%s warning_shortage_amount=%s overstock_candidate_amount=%s demand_surge_rows=%s demand_surge_emergency_rows=%s demand_surge_warning_rows=%s demand_surge_normal_rows=%s adjusted_remaining_demand_qty=%s adjusted_shortage_qty=%s adjusted_shortage_amount=%s evaluation_elapsed_days=%s evaluation_total_days=%s readiness_warning_pct=%s elapsed_ms=%s",
        len(work),
        int(status.eq("긴급 부족").sum()),
        int(status.eq("부족 주의").sum()),
        int(status.eq("적정").sum()),
        int(status.eq("판정 제외").sum()),
        int(overstock_candidate.sum()),
        float(pd.to_numeric(work.loc[status.eq("긴급 부족"), "위험보정부족예상금액"], errors="coerce").fillna(0).sum()),
        float(pd.to_numeric(work.loc[status.eq("부족 주의"), "위험보정부족예상금액"], errors="coerce").fillna(0).sum()),
        float(pd.to_numeric(work.loc[overstock_candidate, "과잉후보금액"], errors="coerce").fillna(0).sum()),
        int(demand_surge.sum()),
        int((demand_surge & status.eq("긴급 부족")).sum()),
        int((demand_surge & status.eq("부족 주의")).sum()),
        int((demand_surge & status.eq("적정")).sum()),
        float(pd.to_numeric(work.loc[demand_surge, "위험보정잔여예상수요"], errors="coerce").fillna(0).sum()),
        float(pd.to_numeric(work.loc[demand_surge, "위험보정부족예상수량"], errors="coerce").fillna(0).sum()),
        float(pd.to_numeric(work.loc[demand_surge, "위험보정부족예상금액"], errors="coerce").fillna(0).sum()),
        int(pd.to_numeric(work.get("평가월경과일수", pd.Series(0, index=work.index)), errors="coerce").fillna(0).max()),
        int(pd.to_numeric(work.get("평가월총일수", pd.Series(0, index=work.index)), errors="coerce").fillna(0).max()),
        float(readiness_warning_pct),
        int((time.perf_counter() - started) * 1000),
    )
    return summary


def _build_sales_facts(
    payload: Mapping[str, Any] | None,
    *,
    history_actuals: list[dict[str, Any]] | None = None,
    evaluation_month: Any = None,
    policy_date: Any = None,
    today: date | None = None,
) -> dict[str, Any]:
    df = _payload_df(payload)
    meta = _payload_meta(payload)
    completed_total = _sum_col(df, "완료월총매출") or _num(meta.get("sum_completed_month_sales_amt"))
    completed_months = int(max([_num(v, 0) for v in df["완료월수"].tolist()], default=0)) if "완료월수" in df.columns else int(_num(meta.get("completed_month_count")))
    completed_avg = _safe_div(completed_total, completed_months)
    current_sales = _sum_col(df, "당월 현재매출") or _num(meta.get("sum_current_month_sales_amt"))
    forecast_sales = _sum_col(df, "당월 예상매출") or _num(meta.get("sum_current_month_expected_sales_amt"))
    progress_pct = _safe_div(current_sales * 100.0, forecast_sales)
    remaining_sales = _sum_col(df, "당월 잔여예상") or _num(meta.get("sum_current_month_remaining_expected_amt"))
    if not ("당월 잔여예상" in df.columns or "sum_current_month_remaining_expected_amt" in meta):
        remaining_sales = max(forecast_sales - current_sales, 0.0)
    evaluation_yyyymm = _normalize_yyyymm(evaluation_month) or _normalize_yyyymm(meta.get("evaluation_month") or meta.get("current_month"))
    time_progress = _dashboard_time_progress(evaluation_yyyymm, policy_date=policy_date, today=today)
    time_progress_pct = time_progress["pct"]
    expected_to_date_sales = (
        float(forecast_sales) * float(time_progress_pct) / 100.0
        if time_progress_pct is not None
        else None
    )
    time_adjusted_achievement_pct = (
        float(progress_pct) / float(time_progress_pct) * 100.0
        if time_progress_pct not in (None, 0)
        else None
    )
    if time_adjusted_achievement_pct is None:
        time_adjusted_status = "계산불가"
    elif time_adjusted_achievement_pct >= 105.0:
        time_adjusted_status = "시간 진척보다 앞섬"
    elif time_adjusted_achievement_pct >= 95.0:
        time_adjusted_status = "시간 진척과 유사"
    else:
        time_adjusted_status = "시간 진척보다 뒤처짐"

    amount_basis_col = "완료월총매출" if "완료월총매출" in df.columns else "총매출액"
    total_basis = _sum_col(df, amount_basis_col)
    top5_share = 0.0
    top10_share = 0.0
    if total_basis > 0 and amount_basis_col in df.columns:
        amount_s = pd.to_numeric(df[amount_basis_col], errors="coerce").fillna(0).sort_values(ascending=False)
        top5_share = float(amount_s.head(5).sum() / total_basis * 100.0)
        top10_share = float(amount_s.head(10).sum() / total_basis * 100.0)

    trend_counts: dict[str, int] = {}
    trend_amounts: dict[str, float] = {}
    trend_shares: dict[str, float] = {}
    if "추세판정" in df.columns:
        trend_labels = _text_series(df, "추세판정", "미분류")
        trend_counts = {str(k): int(v) for k, v in trend_labels.value_counts().to_dict().items()}
        if amount_basis_col in df.columns:
            grouped = df.assign(_trend=trend_labels).groupby("_trend")[amount_basis_col].sum()
            trend_amounts = {str(k): float(v) for k, v in grouped.to_dict().items()}
            trend_shares = {
                str(k): float(_safe_div(v * 100.0, total_basis))
                for k, v in trend_amounts.items()
            }

    decline_targets: list[dict[str, Any]] = []
    if not df.empty:
        work = df.copy()
        trend = _text_series(work, "추세판정", "")
        amount_values = work[amount_basis_col] if amount_basis_col in work.columns else pd.Series([0] * len(work), index=work.index)
        growth_values = work["최근3개월증감률"] if "최근3개월증감률" in work.columns else pd.Series([0] * len(work), index=work.index)
        work["_amount"] = pd.to_numeric(amount_values, errors="coerce").fillna(0)
        work["_growth"] = pd.to_numeric(growth_values, errors="coerce").fillna(0)
        name_col = "제약사명" if "제약사명" in work.columns else work.columns[0]
        risk = work[trend.str.contains("감소", na=False)].sort_values(["_amount", "_growth"], ascending=[False, True])
        for _, row in risk.head(5).iterrows():
            decline_targets.append(
                {
                    "target": str(row.get(name_col) or "미지정"),
                    "amount": float(row.get("_amount") or 0),
                    "growth_pct": float(row.get("_growth") or 0),
                    "reason": "고매출 감소 판정",
                }
            )

    chart_rows: list[dict[str, Any]] = []
    completed_actuals: list[dict[str, Any]] = []
    for col in _month_sales_columns(df):
        period, period_sort = _chart_period(col.replace(" 매출", ""))
        if not period_sort:
            continue
        completed_actuals.append(
            {
                "period": period,
                "period_sort": period_sort,
                "value": _sum_col(df, col),
                "kind": "완료월 실제",
                "partial_period": False,
                "forecast_basis": "",
                "forecast_status": "실제",
            }
        )
    chart_rows.extend(completed_actuals)
    chart_rows.extend(_completed_month_pre_forecasts(completed_actuals, history_actuals=history_actuals))
    if current_sales or forecast_sales:
        period, period_sort = _chart_period(meta.get("evaluation_month") or meta.get("current_month") or "")
        if period_sort:
            chart_rows.append(
                {
                    "period": period,
                    "period_sort": period_sort,
                    "value": current_sales,
                    "kind": "당월 현재(부분월)",
                    "partial_period": True,
                    "forecast_basis": "당월 현재값",
                    "forecast_status": "부분월 실제",
                }
            )
            chart_rows.append(
                {
                    "period": period,
                    "period_sort": period_sort,
                    "value": forecast_sales,
                    "kind": "당월 예상",
                    "partial_period": True,
                    "forecast_basis": "당월 예상",
                    "forecast_status": "당월 예상",
                }
            )
    chart_rows.sort(key=lambda row: (str(row.get("period_sort") or ""), str(row.get("kind") or "")))

    return {
        "metrics": {
            "completed_month_avg_sales": _fact(
                "완료월 평균매출",
                completed_avg,
                unit="원",
                aggregation="sum / completed_month_count",
                grain="제약사-완료월",
                time_basis=f"완료월 {completed_months}개월",
                source_columns=["완료월총매출", "완료월수"],
            ),
            "current_month_sales": _fact(
                "당월 현재매출",
                current_sales,
                unit="원",
                aggregation="sum",
                grain="제약사",
                time_basis="당월 부분월 현재값",
                source_columns=["당월 현재매출"],
                partial_period=True,
                comparable_with=["당월 예상매출"],
            ),
            "current_month_forecast_sales": _fact(
                "당월 예상매출",
                forecast_sales,
                unit="원",
                aggregation="sum",
                grain="제약사",
                time_basis="당월 월말 예상값",
                source_columns=["당월 예상매출"],
                partial_period=True,
                comparable_with=["당월 현재매출"],
            ),
            "current_month_remaining_forecast_sales": _fact(
                "당월 잔여예상",
                remaining_sales,
                unit="원",
                aggregation="forecast - current when source value is unavailable",
                grain="제약사",
                time_basis="당월 잔여 예상 매출",
                source_columns=["당월 잔여예상", "당월 예상매출", "당월 현재매출"],
                partial_period=True,
            ),
            "current_month_progress_pct": _fact(
                "당월 진척률",
                progress_pct,
                unit="%",
                aggregation="current / forecast",
                grain="제약사",
                time_basis="당월 부분월",
                source_columns=["당월 현재매출", "당월 예상매출"],
                partial_period=True,
            ),
            "time_progress_pct": _fact(
                "시간 진척률",
                time_progress_pct,
                unit="%",
                aggregation="elapsed_days / calendar_days",
                grain="평가월",
                time_basis=(
                    f"{evaluation_yyyymm or '평가월'} "
                    f"{time_progress.get('elapsed_days')}/{time_progress.get('total_days')}일 경과 "
                    f"{time_progress.get('status') or ''}"
                ),
                source_columns=[],
                partial_period=True,
            ),
            "time_adjusted_achievement_pct": _fact(
                "시간 대비 달성률",
                time_adjusted_achievement_pct,
                unit="%",
                aggregation="sales_progress / time_progress",
                grain="평가월",
                time_basis=time_adjusted_status,
                source_columns=["당월 현재매출", "당월 예상매출"],
                partial_period=True,
            ),
            "top5_concentration_pct": _fact(
                "상위 5개 매출 집중도",
                top5_share,
                unit="%",
                aggregation="top5 / total",
                grain="제약사",
                time_basis="완료월 매출 기준",
                source_columns=[amount_basis_col],
            ),
            "top10_concentration_pct": _fact(
                "상위 10개 매출 집중도",
                top10_share,
                unit="%",
                aggregation="top10 / total",
                grain="제약사",
                time_basis="완료월 매출 기준",
                source_columns=[amount_basis_col],
            ),
        },
        "chart_rows": chart_rows,
        "visualization": {
            "current_sales": current_sales,
            "forecast_sales": forecast_sales,
            "remaining_forecast": remaining_sales,
            "sales_progress_pct": progress_pct,
            "time_progress_pct": time_progress_pct,
            "time_adjusted_achievement_pct": time_adjusted_achievement_pct,
            "time_adjusted_status": time_adjusted_status,
            "expected_to_date_sales": expected_to_date_sales,
            "evaluation_month": evaluation_yyyymm,
            "chart_month_count": len({str(row.get("period_sort") or "") for row in chart_rows if row.get("period_sort")}),
            "completed_month_count": completed_months,
        },
        "trend_counts": trend_counts,
        "trend_amounts": trend_amounts,
        "trend_shares": trend_shares,
        "decline_targets": decline_targets,
        "data_quality": [] if not df.empty else ["제약사별 매출 추세 요약 자료 없음"],
    }


def _attach_major_purchase_vendors(
    rows: list[dict[str, Any]],
    purchase_vendor_df: pd.DataFrame | None,
    *,
    evaluation_month: str,
    history_month_from: str,
    source_call_count: int,
) -> dict[str, Any]:
    """Assign exactly one completed-month purchase vendor to each product row."""
    started = time.perf_counter()
    recent_from = _add_months(evaluation_month, -6)
    recent_to = _add_months(evaluation_month, -1)
    columns = ["기준월", "제품코드", "매입처코드", "매입처명", "입고수량", "매입금액", "매입발생건수"]
    source = purchase_vendor_df if isinstance(purchase_vendor_df, pd.DataFrame) else pd.DataFrame(columns=columns)
    work = source[[c for c in columns if c in source.columns]].copy()
    for col in columns:
        if col not in work.columns:
            work[col] = "" if col in {"기준월", "제품코드", "매입처코드", "매입처명"} else 0.0
    for col in ("기준월", "제품코드", "매입처코드", "매입처명"):
        work[col] = work[col].fillna("").astype(str).str.strip()
    numeric_invalid = pd.Series(False, index=work.index)
    for col in ("입고수량", "매입금액", "매입발생건수"):
        raw_value = work[col]
        numeric_value = pd.to_numeric(raw_value, errors="coerce")
        nonempty_value = raw_value.notna() & raw_value.astype(str).str.strip().ne("")
        numeric_invalid |= nonempty_value & numeric_value.isna()
        work[col] = numeric_value.fillna(0.0)

    product_missing = work["제품코드"].eq("")
    month_missing = work["기준월"].eq("")
    month_valid = work["기준월"].str.fullmatch(r"\d{6}", na=False)
    period_valid = (
        month_valid
        & work["기준월"].between(str(history_month_from or "000000"), str(evaluation_month or "999999"))
        & work["기준월"].lt(str(evaluation_month or ""))
    )
    classification = pd.Series("classified", index=work.index, dtype="object")
    classification.loc[product_missing] = "missing_product_code"
    classification.loc[classification.eq("classified") & month_missing] = "missing_month"
    classification.loc[classification.eq("classified") & numeric_invalid] = "invalid_numeric"
    classification.loc[classification.eq("classified") & ~period_valid] = "other_excluded"
    purchase_unclassified_rows = int(classification.ne("classified").sum())
    missing_product_code_rows = int(classification.eq("missing_product_code").sum())
    missing_month_rows = int(classification.eq("missing_month").sum())
    invalid_numeric_rows = int(classification.eq("invalid_numeric").sum())
    other_excluded_rows = int(classification.eq("other_excluded").sum())
    work = work.loc[classification.eq("classified")].copy()
    aggregate_started = time.perf_counter()
    purchase_positive_rows = 0
    purchase_nonpositive_rows = 0
    if not work.empty:
        work["_positive_purchase"] = (work["매입금액"] > 1e-9) | (work["입고수량"] > 1e-9)
        purchase_positive_rows = int(work["_positive_purchase"].sum())
        purchase_nonpositive_rows = int(len(work) - purchase_positive_rows)
        recent_mask = work["기준월"].between(recent_from, recent_to)
        # 최근 6완료월 합계는 사전에 만든 최소 수치 열만 집계한다.
        # groupby 안에서 원본 전체 frame을 다시 인덱싱하지 않아도 된다.
        work["_recent_purchase_amount"] = work["매입금액"].where(recent_mask, 0.0)
        work["_recent_inbound_qty"] = work["입고수량"].where(recent_mask, 0.0)
        work["_recent_purchase_event_count"] = work["매입발생건수"].where(recent_mask, 0.0)
        grouped = (
            work.groupby(["제품코드", "매입처코드", "매입처명"], dropna=False, as_index=False)
            .agg(
                최근6완료월순매입금액=("_recent_purchase_amount", "sum"),
                최근6완료월순입고수량=("_recent_inbound_qty", "sum"),
                최근6완료월매입발생건수=("_recent_purchase_event_count", "sum"),
                지원기간순매입금액=("매입금액", "sum"),
                지원기간순입고수량=("입고수량", "sum"),
                지원기간매입발생건수=("매입발생건수", "sum"),
            )
        )
        positive = work.loc[work["_positive_purchase"]].copy()
        if not positive.empty:
            latest = positive.groupby(["제품코드", "매입처코드", "매입처명"], dropna=False)["기준월"].max().rename("지원기간최근매입월").reset_index()
            recent_latest = positive.loc[positive["기준월"].between(recent_from, recent_to)].groupby(["제품코드", "매입처코드", "매입처명"], dropna=False)["기준월"].max().rename("최근6완료월최근매입월").reset_index()
            grouped = grouped.merge(latest, on=["제품코드", "매입처코드", "매입처명"], how="left")
            grouped = grouped.merge(recent_latest, on=["제품코드", "매입처코드", "매입처명"], how="left")
        else:
            grouped["지원기간최근매입월"] = ""
            grouped["최근6완료월최근매입월"] = ""
    else:
        grouped = pd.DataFrame(columns=["제품코드", "매입처코드", "매입처명"])
    aggregate_ms = int((time.perf_counter() - aggregate_started) * 1000)

    assignments: dict[str, dict[str, Any]] = {}
    rank_started = time.perf_counter()
    if not grouped.empty:
        for col in ("최근6완료월순매입금액", "최근6완료월순입고수량", "지원기간순매입금액", "지원기간순입고수량"):
            grouped[col] = pd.to_numeric(grouped.get(col, 0), errors="coerce").fillna(0.0)
        grouped["_recent_positive"] = (grouped["최근6완료월순매입금액"] > 1e-9) | (grouped["최근6완료월순입고수량"] > 1e-9)
        grouped["_history_positive"] = (grouped["지원기간순매입금액"] > 1e-9) | (grouped["지원기간순입고수량"] > 1e-9)
        grouped["매입처코드"] = grouped["매입처코드"].fillna("").astype(str).str.strip()
        grouped["_vendor_code_present"] = grouped["매입처코드"].ne("")
        product_group = grouped.groupby("제품코드", sort=False)
        grouped["_recent_positive_exists"] = product_group["_recent_positive"].transform("any")
        grouped["_history_positive_exists"] = product_group["_history_positive"].transform("any")
        grouped["_recent_coded_positive_exists"] = (
            (grouped["_recent_positive"] & grouped["_vendor_code_present"])
            .groupby(grouped["제품코드"], sort=False)
            .transform("any")
        )
        grouped["_history_coded_positive_exists"] = (
            (grouped["_history_positive"] & grouped["_vendor_code_present"])
            .groupby(grouped["제품코드"], sort=False)
            .transform("any")
        )
        recent_candidate = grouped["_recent_positive"] & (
            ~grouped["_recent_coded_positive_exists"] | grouped["_vendor_code_present"]
        )
        history_candidate = (
            ~grouped["_recent_positive_exists"]
            & grouped["_history_positive"]
            & (~grouped["_history_coded_positive_exists"] | grouped["_vendor_code_present"])
        )
        grouped["_candidate_tier"] = 0
        grouped.loc[history_candidate, "_candidate_tier"] = 2
        grouped.loc[recent_candidate, "_candidate_tier"] = 1
        candidates = grouped.loc[grouped["_candidate_tier"].gt(0)].copy()
        if not candidates.empty:
            recent_tier = candidates["_candidate_tier"].eq(1)
            candidates["_amount_rank"] = candidates["최근6완료월순매입금액"].where(
                recent_tier,
                candidates["지원기간순매입금액"],
            ).clip(lower=0)
            candidates["_qty_rank"] = candidates["최근6완료월순입고수량"].where(
                recent_tier,
                candidates["지원기간순입고수량"],
            ).clip(lower=0)
            candidates["_month_rank"] = candidates["최근6완료월최근매입월"].where(
                recent_tier,
                candidates["지원기간최근매입월"],
            ).fillna("").astype(str)
            candidates["주요매입처선정기준"] = candidates["_candidate_tier"].map(
                {1: "최근 6완료월", 2: "지원기간 fallback"}
            )
            winners = candidates.sort_values(
                ["제품코드", "_candidate_tier", "_amount_rank", "_qty_rank", "_month_rank", "매입처코드"],
                ascending=[True, True, False, False, False, True],
                kind="stable",
            ).drop_duplicates(subset=["제품코드"], keep="first")
            assignments = {
                str(product_code or "").strip(): winner
                for product_code, winner in winners.set_index("제품코드").to_dict("index").items()
            }
    rank_ms = int((time.perf_counter() - rank_started) * 1000)

    status_risk_rows: list[dict[str, Any]] = []
    risk_rows: list[dict[str, Any]] = []
    for row in rows:
        product_code = str(row.get("product_code") or "").strip()
        winner = assignments.get(product_code) if product_code else None
        if not product_code:
            status = "product_code_missing"
            vendor_name = "매입처 미확인"
        elif not winner:
            status = "recent_purchase_none"
            vendor_name = "최근 매입 없음"
        elif not str(winner.get("매입처코드") or "").strip():
            status = "vendor_unknown"
            vendor_name = "매입처 미확인"
        else:
            status = "assigned"
            vendor_name = str(winner.get("매입처명") or "").strip() or str(winner.get("매입처코드") or "").strip()
        row.update(
            {
                "주요매입처코드": str((winner or {}).get("매입처코드") or "").strip(),
                "주요매입처명": vendor_name,
                "주요매입처상태": status,
                "주요매입처선정기준": str((winner or {}).get("주요매입처선정기준") or ""),
                "최근6완료월순매입금액": float((winner or {}).get("최근6완료월순매입금액") or 0),
                "최근6완료월순입고수량": float((winner or {}).get("최근6완료월순입고수량") or 0),
                "최근6완료월최근매입월": str((winner or {}).get("최근6완료월최근매입월") or ""),
                "지원기간순매입금액": float((winner or {}).get("지원기간순매입금액") or 0),
                "지원기간순입고수량": float((winner or {}).get("지원기간순입고수량") or 0),
                "지원기간최근매입월": str((winner or {}).get("지원기간최근매입월") or ""),
                "주요매입처자료완전": bool(winner and status == "assigned"),
                "주요매입처미확인사유": "" if status == "assigned" else status,
            }
        )
        if row.get("재고위험상태") in {"긴급 부족", "부족 주의"}:
            status_risk_rows.append(row)
            if float(row.get("위험보정부족예상금액") or 0) > 0:
                risk_rows.append(row)

    vendor_group_started = time.perf_counter()
    assigned = [r for r in risk_rows if r.get("주요매입처상태") == "assigned"]
    unassigned = [r for r in risk_rows if r.get("주요매입처상태") != "assigned"]
    vendor_rows: list[dict[str, Any]] = []
    if assigned:
        vendor_df = pd.DataFrame(assigned)
        for (code, name), group in vendor_df.groupby(["주요매입처코드", "주요매입처명"], dropna=False, sort=False):
            emergency = group.loc[group["재고위험상태"].eq("긴급 부족")]
            warning = group.loc[group["재고위험상태"].eq("부족 주의")]
            vendor_rows.append(
                {
                    "주요매입처코드": str(code or ""),
                    "주요매입처명": str(name or ""),
                    "긴급부족품목수": int(len(emergency)),
                    "부족주의품목수": int(len(warning)),
                    "위험품목수": int(len(group)),
                    "긴급부족금액": float(emergency["위험보정부족예상금액"].sum()),
                    "부족주의금액": float(warning["위험보정부족예상금액"].sum()),
                    "전체위험보정부족금액": float(group["위험보정부족예상금액"].sum()),
                    "위험보정부족예상수량": float(group["위험보정부족예상수량"].sum()),
                    "수요급증품목수": int(group.get("수요급증여부", pd.Series(False, index=group.index)).fillna(False).astype(bool).sum()),
                    "과잉후보품목수": int(group.get("과잉후보여부", pd.Series(False, index=group.index)).fillna(False).astype(bool).sum()),
                    "과잉후보금액": float(pd.to_numeric(group.get("과잉후보금액", 0), errors="coerce").fillna(0).sum()),
                }
            )
    vendor_rows.sort(key=lambda r: (-float(r["전체위험보정부족금액"]), -float(r["긴급부족금액"]), -int(r["위험품목수"]), str(r["주요매입처코드"])))
    vendor_risk_group_ms = int((time.perf_counter() - vendor_group_started) * 1000)
    total_amount = float(sum(float(r.get("위험보정부족예상금액") or 0) for r in risk_rows))
    assigned_amount = float(sum(float(r.get("위험보정부족예상금액") or 0) for r in assigned))
    status_emergency_rows = [row for row in status_risk_rows if row.get("재고위험상태") == "긴급 부족"]
    status_warning_rows = [row for row in status_risk_rows if row.get("재고위험상태") == "부족 주의"]
    amount_positive_emergency_rows = [row for row in risk_rows if row.get("재고위험상태") == "긴급 부족"]
    amount_positive_warning_rows = [row for row in risk_rows if row.get("재고위험상태") == "부족 주의"]
    summary = {
        "inventory_rows": int(len(rows)), "risk_rows": int(len(risk_rows)), "assigned_rows": int(len(assigned)), "unassigned_rows": int(len(unassigned)),
        "vendor_count": int(len(vendor_rows)), "top_vendor_count": int(min(10, len(vendor_rows))),
        "emergency_rows": int(len(amount_positive_emergency_rows)), "warning_rows": int(len(amount_positive_warning_rows)),
        "status_risk_rows": int(len(status_risk_rows)), "status_emergency_rows": int(len(status_emergency_rows)), "status_warning_rows": int(len(status_warning_rows)),
        "amount_positive_risk_rows": int(len(risk_rows)), "amount_positive_emergency_rows": int(len(amount_positive_emergency_rows)), "amount_positive_warning_rows": int(len(amount_positive_warning_rows)),
        "amount_zero_risk_rows": int(len(status_risk_rows) - len(risk_rows)),
        "amount_zero_emergency_rows": int(len(status_emergency_rows) - len(amount_positive_emergency_rows)),
        "amount_zero_warning_rows": int(len(status_warning_rows) - len(amount_positive_warning_rows)),
        "total_adjusted_shortage_amount": total_amount, "assigned_adjusted_shortage_amount": assigned_amount, "unassigned_adjusted_shortage_amount": total_amount - assigned_amount,
        "recent_purchase_none_rows": int(sum(r.get("주요매입처상태") == "recent_purchase_none" for r in unassigned)), "recent_purchase_none_amount": float(sum(float(r.get("위험보정부족예상금액") or 0) for r in unassigned if r.get("주요매입처상태") == "recent_purchase_none")),
        "vendor_unknown_rows": int(sum(r.get("주요매입처상태") in {"vendor_unknown", "product_code_missing"} for r in unassigned)), "vendor_unknown_amount": float(sum(float(r.get("위험보정부족예상금액") or 0) for r in unassigned if r.get("주요매입처상태") in {"vendor_unknown", "product_code_missing"})),
        "basis_completed_month_count": 6, "basis_month_from": recent_from, "basis_month_to": recent_to,
        "purchase_source_rows": int(len(source)), "purchase_positive_rows": int(purchase_positive_rows), "purchase_nonpositive_rows": int(purchase_nonpositive_rows),
        "purchase_unclassified_rows": int(purchase_unclassified_rows), "missing_product_code_rows": int(missing_product_code_rows),
        "missing_month_rows": int(missing_month_rows), "invalid_numeric_rows": int(invalid_numeric_rows), "other_excluded_rows": int(other_excluded_rows),
    }
    log.info("[dashboard.vendor_stock_risk] inventory_rows=%s risk_rows=%s assigned_rows=%s unassigned_rows=%s vendor_rows=%s status_risk_rows=%s status_emergency_rows=%s status_warning_rows=%s amount_positive_risk_rows=%s amount_positive_emergency_rows=%s amount_positive_warning_rows=%s amount_zero_risk_rows=%s amount_zero_emergency_rows=%s amount_zero_warning_rows=%s total_adjusted_shortage_amount=%s assigned_adjusted_shortage_amount=%s unassigned_adjusted_shortage_amount=%s recent_purchase_none_rows=%s vendor_unknown_rows=%s top_vendor_count=%s purchase_source_rows=%s purchase_positive_rows=%s purchase_nonpositive_rows=%s purchase_unclassified_rows=%s missing_product_code_rows=%s missing_month_rows=%s invalid_numeric_rows=%s other_excluded_rows=%s major_vendor_aggregate_ms=%s major_vendor_rank_ms=%s vendor_risk_group_ms=%s basis_month_from=%s basis_month_to=%s source_call_count=%s elapsed_ms=%s", summary["inventory_rows"], summary["risk_rows"], summary["assigned_rows"], summary["unassigned_rows"], summary["vendor_count"], summary["status_risk_rows"], summary["status_emergency_rows"], summary["status_warning_rows"], summary["amount_positive_risk_rows"], summary["amount_positive_emergency_rows"], summary["amount_positive_warning_rows"], summary["amount_zero_risk_rows"], summary["amount_zero_emergency_rows"], summary["amount_zero_warning_rows"], summary["total_adjusted_shortage_amount"], summary["assigned_adjusted_shortage_amount"], summary["unassigned_adjusted_shortage_amount"], summary["recent_purchase_none_rows"], summary["vendor_unknown_rows"], summary["top_vendor_count"], summary["purchase_source_rows"], summary["purchase_positive_rows"], summary["purchase_nonpositive_rows"], summary["purchase_unclassified_rows"], summary["missing_product_code_rows"], summary["missing_month_rows"], summary["invalid_numeric_rows"], summary["other_excluded_rows"], aggregate_ms, rank_ms, vendor_risk_group_ms, recent_from, recent_to, int(source_call_count), int((time.perf_counter() - started) * 1000))
    return {"summary": summary, "rows": vendor_rows, "top_rows": vendor_rows[:10], "aggregate_ms": aggregate_ms, "rank_ms": rank_ms, "group_ms": vendor_risk_group_ms}


def _attach_stock_extension_facts(
    rows: list[dict[str, Any]],
    *,
    evaluation_remaining_days: int | None,
) -> dict[str, Any]:
    """Attach display-only stock-cover and evidence facts from existing rows.

    The shared sales bundle is monthly, so it cannot truthfully provide a last
    normal-outbound date. Keep that boundary explicit instead of inventing a
    date or adding a source call.
    """
    started = time.perf_counter()
    if not rows:
        summary = {
            "inventory_rows": 0,
            "ready_rows": 0,
            "zero_stock_rows": 0,
            "no_demand_rows": 0,
            "insufficient_data_rows": 0,
            "closed_horizon_rows": 0,
            "outbound_source_required_rows": 0,
            "additional_source_call_count": 0,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
        log.info(
            "[dashboard.stock_extension] inventory_rows=0 ready_rows=0 zero_stock_rows=0 no_demand_rows=0 insufficient_data_rows=0 closed_horizon_rows=0 outbound_source_required_rows=0 additional_source_call_count=0 elapsed_ms=%s",
            summary["elapsed_ms"],
        )
        return summary

    work = pd.DataFrame(rows)
    index = work.index
    stock = pd.to_numeric(work.get("current_stock_qty", pd.Series(float("nan"), index=index)), errors="coerce")
    demand = pd.to_numeric(
        work.get("위험보정잔여예상수요", work.get("remaining_expected_demand_qty", pd.Series(float("nan"), index=index))),
        errors="coerce",
    )
    stock_present = work.get("_stock_cover_stock_present", stock.notna()).fillna(False).astype(bool)
    demand_present = work.get("_stock_cover_demand_present", demand.notna()).fillna(False).astype(bool)
    required = stock_present & demand_present & stock.notna() & demand.notna()
    try:
        remaining_days = int(evaluation_remaining_days) if evaluation_remaining_days is not None else None
    except (TypeError, ValueError):
        remaining_days = None

    status = pd.Series("insufficient_data", index=index, dtype="object")
    cover_days = pd.Series(float("nan"), index=index, dtype="float64")
    daily_demand = pd.Series(float("nan"), index=index, dtype="float64")
    if remaining_days is not None and remaining_days <= 0:
        status.loc[:] = "closed_horizon"
    elif remaining_days is not None:
        daily_demand = demand / float(remaining_days)
        no_demand = required & demand.le(0)
        positive_demand = required & demand.gt(0)
        zero_stock = positive_demand & stock.le(0)
        ready = positive_demand & stock.gt(0)
        status.loc[no_demand] = "no_demand"
        status.loc[zero_stock] = "zero_stock"
        status.loc[ready] = "ready"
        cover_days.loc[zero_stock] = 0.0
        cover_days.loc[ready] = stock.loc[ready] / daily_demand.loc[ready]

    status_labels = {
        "ready": "계산 가능",
        "zero_stock": "재고 없음",
        "no_demand": "잔여 예상수요 없음",
        "insufficient_data": "자료 부족",
        "closed_horizon": "평가기간 종료",
    }
    overstock_candidate = work.get("과잉후보여부", pd.Series(False, index=index)).fillna(False).astype(bool)
    overstock_reason = work.get("과잉후보사유", pd.Series("", index=index, dtype="object")).fillna("").astype(str).str.strip()
    evidence = pd.Series("기존 과잉후보 기준 미해당", index=index, dtype="object")
    evidence.loc[overstock_candidate] = overstock_reason.loc[overstock_candidate].where(
        overstock_reason.loc[overstock_candidate].ne(""),
        "기존 과잉후보 기준 해당",
    )

    work["stock_cover_days"] = cover_days.astype("object").where(cover_days.notna(), None)
    work["stock_cover_daily_demand_qty"] = daily_demand.astype("object").where(daily_demand.notna(), None)
    work["stock_cover_remaining_days"] = remaining_days
    work["stock_cover_status"] = status
    work["재고커버일"] = work["stock_cover_days"]
    work["재고커버 자료상태"] = status.map(status_labels).fillna("자료 부족")
    work["과잉·저활성 근거"] = evidence
    work["last_normal_outbound_date"] = ""
    work["outbound_elapsed_days"] = None
    work["outbound_data_status"] = "source_required"
    work["최근 정상 출고일"] = ""
    work["출고 경과일"] = None
    work["출고 자료상태"] = "자료 연결 필요"
    rows[:] = work.drop(columns=["_stock_cover_stock_present", "_stock_cover_demand_present"], errors="ignore").to_dict("records")

    counts = status.value_counts(dropna=False)
    summary = {
        "inventory_rows": int(len(work)),
        "ready_rows": int(counts.get("ready", 0)),
        "zero_stock_rows": int(counts.get("zero_stock", 0)),
        "no_demand_rows": int(counts.get("no_demand", 0)),
        "insufficient_data_rows": int(counts.get("insufficient_data", 0)),
        "closed_horizon_rows": int(counts.get("closed_horizon", 0)),
        "outbound_source_required_rows": int(len(work)),
        "additional_source_call_count": 0,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
    log.info(
        "[dashboard.stock_extension] inventory_rows=%s ready_rows=%s zero_stock_rows=%s no_demand_rows=%s insufficient_data_rows=%s closed_horizon_rows=%s outbound_source_required_rows=%s additional_source_call_count=0 elapsed_ms=%s",
        summary["inventory_rows"], summary["ready_rows"], summary["zero_stock_rows"],
        summary["no_demand_rows"], summary["insufficient_data_rows"], summary["closed_horizon_rows"],
        summary["outbound_source_required_rows"], summary["elapsed_ms"],
    )
    return summary


def _build_visual_phase2_summary(
    rows: list[dict[str, Any]],
    purchase_vendor_df: pd.DataFrame | None,
    *,
    evaluation_remaining_days: int | None,
    today_action_count: int = 0,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build bounded presentation aggregates from already-loaded Dashboard facts."""
    started = time.perf_counter()
    work = pd.DataFrame(rows)
    index = work.index
    status = work.get("stock_cover_status", pd.Series("insufficient_data", index=index)).fillna("insufficient_data").astype(str)
    cover_days = pd.to_numeric(work.get("stock_cover_days", pd.Series(float("nan"), index=index)), errors="coerce")
    remaining = int(evaluation_remaining_days or 0)
    cover_zero = status.eq("zero_stock")
    cover_ready = status.eq("ready")
    cover_shortfall = cover_ready & cover_days.lt(remaining)
    cover_sufficient = cover_ready & ~cover_shortfall
    cover_no_demand = status.eq("no_demand")
    cover_insufficient = status.isin(["insufficient_data", "closed_horizon"])
    cover_total = int(len(work))
    cover_counts = {
        "zero_stock": int(cover_zero.sum()),
        "shortfall": int(cover_shortfall.sum()),
        "sufficient": int(cover_sufficient.sum()),
        "no_demand": int(cover_no_demand.sum()),
        "insufficient": int(cover_insufficient.sum()),
    }
    cover_valid = sum(cover_counts.values()) == cover_total
    inbound_delay = work.get("inbound_delayed_candidate", pd.Series(False, index=index)).fillna(False).astype(bool)
    overstock = work.get("과잉후보여부", pd.Series(False, index=index)).fillna(False).astype(bool)
    recent_none = work.get("주요매입처상태", pd.Series("", index=index)).fillna("").astype(str).eq("recent_purchase_none")
    vendor_unknown = work.get("주요매입처상태", pd.Series("", index=index)).fillna("").astype(str).isin(["vendor_unknown", "product_code_missing"])
    demand_surge = work.get("수요급증여부", pd.Series(False, index=index)).fillna(False).astype(bool)
    purchase_rows: list[dict[str, Any]] = []
    if isinstance(purchase_vendor_df, pd.DataFrame) and {"기준월", "매입금액"}.issubset(purchase_vendor_df.columns):
        purchase = purchase_vendor_df.loc[:, ["기준월", "매입금액"]].copy()
        purchase["기준월"] = purchase["기준월"].map(_normalize_yyyymm)
        purchase["매입금액"] = pd.to_numeric(purchase["매입금액"], errors="coerce").fillna(0.0)
        purchase = purchase.loc[purchase["기준월"].ne("")]
        purchase_monthly = purchase.groupby("기준월", as_index=False)["매입금액"].sum().sort_values("기준월", kind="stable").tail(18)
        purchase_rows = [
            {"month": str(row.get("기준월") or ""), "amount": float(row.get("매입금액") or 0.0)}
            for row in purchase_monthly.to_dict("records")
        ]
    summary = {
        "inventory_count": cover_total,
        "evaluation_remaining_days": remaining,
        "cover_zero_stock_count": cover_counts["zero_stock"],
        "cover_shortfall_count": cover_counts["shortfall"],
        "cover_sufficient_count": cover_counts["sufficient"],
        "cover_no_demand_count": cover_counts["no_demand"],
        "cover_insufficient_count": cover_counts["insufficient"],
        "cover_partition_valid": cover_valid,
        "inbound_delay_candidate_count": int(inbound_delay.sum()),
        "overstock_candidate_count": int(overstock.sum()),
        "recent_purchase_none_count": int(recent_none.sum()),
        "vendor_unknown_count": int(vendor_unknown.sum()),
        "demand_surge_count": int(demand_surge.sum()),
        "action_count": int(today_action_count),
        "purchase_trend_status": "ready" if purchase_rows else "source_required",
        "purchase_trend_points": int(len(purchase_rows)),
        "additional_source_call_count": 0,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
    return summary, purchase_rows


def _build_visual_phase2_briefing(
    summary: Mapping[str, Any],
    *,
    emergency_count: int,
    warning_count: int,
) -> list[str]:
    """Create short deterministic briefing lines from persisted aggregates only."""
    inventory_count = int(summary.get("inventory_count") or 0)
    lines = [
        f"재고 위험은 긴급 부족 {int(emergency_count):,}개, 부족 주의 {int(warning_count):,}개로 확인됩니다.",
    ]
    if bool(summary.get("cover_partition_valid")):
        lines.append(
            "재고커버 기준으로 재고 없음 "
            f"{int(summary.get('cover_zero_stock_count') or 0):,}개, 잔여 기간 미만 "
            f"{int(summary.get('cover_shortfall_count') or 0):,}개를 우선 확인합니다."
        )
    else:
        lines.append(f"재고커버 집계는 현재 {inventory_count:,}개 품목의 자료 상태를 함께 확인해야 합니다.")
    followups = [
        ("입고 지연 후보", int(summary.get("inbound_delay_candidate_count") or 0)),
        ("과잉 후보", int(summary.get("overstock_candidate_count") or 0)),
        ("최근 매입 없음", int(summary.get("recent_purchase_none_count") or 0)),
        ("매입처 미확인", int(summary.get("vendor_unknown_count") or 0)),
    ]
    active = [f"{label} {count:,}개" for label, count in followups if count > 0]
    lines.append("후속 확인 항목은 " + (", ".join(active) if active else "현재 집계된 후보가 없습니다") + "입니다.")
    surge_count = int(summary.get("demand_surge_count") or 0)
    if surge_count > 0:
        lines.append(f"수요급증 {surge_count:,}개 품목은 현재 출고속도를 반영한 위험보정 수요를 사용합니다.")
    lines.append(f"오늘의 우선 조치 {int(summary.get('action_count') or 0):,}건은 아래 목록에서 확인합니다.")
    return lines[:5]


_RISK_DETAIL_COLUMNS = (
    "위험상태", "위험사유", "제품코드", "제품명", "규격", "제조사명", "제품그룹명", "제품구분명", "제품분류명",
    "주요매입처코드", "주요매입처명", "주요매입처상태", "주요매입처선정기준", "재고기준",
    "현재재고수량", "현재재고금액", "재고평가단가", "당월현재출고수량", "당월기준예상출고수량",
    "진행속도기준월말예상출고수량", "위험보정예상출고수량", "위험보정잔여예상수요", "위험보정재고준비율",
    "위험보정부족예상수량", "위험보정부족예상금액", "재고커버일", "재고커버 자료상태", "과잉후보여부", "과잉·저활성 근거",
    "최근 정상 출고일", "출고 경과일", "출고 자료상태", "수요급증여부", "수요급증상위분류", "수요급증세부분류",
    "수요급증사유", "최근6완료월순매입금액", "최근6완료월순입고수량", "최근6완료월최근매입월",
)


def _risk_detail_vendor_key(row: Mapping[str, Any]) -> str:
    status = str(row.get("주요매입처상태") or "").strip()
    code = str(row.get("주요매입처코드") or "").strip()
    if status == "assigned" and code:
        return f"assigned:{code}"
    if status == "recent_purchase_none":
        return "recent_purchase_none"
    return "vendor_unknown"


def _build_dashboard_risk_detail(
    rows: list[dict[str, Any]],
    *,
    stock_mode: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build the minimal, status-based risk-detail rows without duplicating readiness facts."""
    stock_basis = "장부재고" if str(stock_mode or "").strip() == "book" else "실재고"
    detail_rows: list[dict[str, Any]] = []
    for row in rows:
        risk_status = str(row.get("재고위험상태") or "")
        if risk_status not in {"긴급 부족", "부족 주의"}:
            continue
        detail = {
            "위험상태": risk_status,
            "위험사유": str(row.get("재고위험사유") or ""),
            "제품코드": str(row.get("product_code") or ""),
            "제품명": str(row.get("product_name") or ""),
            "규격": str(row.get("규격") or ""),
            "제조사명": str(row.get("manufacturer_name") or ""),
            "제품그룹명": str(row.get("제품그룹명") or ""),
            "제품구분명": str(row.get("제품구분명") or ""),
            "제품분류명": str(row.get("제품분류명") or ""),
            "주요매입처코드": str(row.get("주요매입처코드") or ""),
            "주요매입처명": str(row.get("주요매입처명") or ""),
            "주요매입처상태": str(row.get("주요매입처상태") or ""),
            "주요매입처선정기준": str(row.get("주요매입처선정기준") or ""),
            "재고기준": stock_basis,
            "현재재고수량": float(row.get("current_stock_qty") or 0),
            "현재재고금액": float(row.get("current_stock_amt") or 0),
            "재고평가단가": float(row.get("stock_valuation_unit_price") or 0),
            "당월현재출고수량": float(row.get("당월현재출고수량") or 0),
            "당월기준예상출고수량": float(row.get("당월기준예상출고수량") or 0),
            "진행속도기준월말예상출고수량": float(row.get("진행속도기준월말예상출고수량") or 0),
            "위험보정예상출고수량": float(row.get("위험보정예상출고수량") or 0),
            "위험보정잔여예상수요": float(row.get("위험보정잔여예상수요") or 0),
            "위험보정재고준비율": float(row.get("위험보정재고준비율") or 0),
            "위험보정부족예상수량": float(row.get("위험보정부족예상수량") or 0),
            "위험보정부족예상금액": float(row.get("위험보정부족예상금액") or 0),
            "재고커버일": row.get("stock_cover_days"),
            "재고커버 자료상태": str(row.get("재고커버 자료상태") or "자료 부족"),
            "과잉후보여부": bool(row.get("과잉후보여부")),
            "과잉·저활성 근거": str(row.get("과잉·저활성 근거") or ""),
            "최근 정상 출고일": str(row.get("last_normal_outbound_date") or ""),
            "출고 경과일": row.get("outbound_elapsed_days"),
            "출고 자료상태": str(row.get("출고 자료상태") or "자료 연결 필요"),
            "수요급증여부": bool(row.get("수요급증여부")),
            "수요급증상위분류": str(row.get("수요급증상위분류") or ""),
            "수요급증세부분류": str(row.get("수요급증세부분류") or ""),
            "수요급증사유": str(row.get("수요급증사유") or ""),
            "최근6완료월순매입금액": float(row.get("최근6완료월순매입금액") or 0),
            "최근6완료월순입고수량": float(row.get("최근6완료월순입고수량") or 0),
            "최근6완료월최근매입월": str(row.get("최근6완료월최근매입월") or ""),
            "최근 정상 입고일": str(row.get("last_normal_inbound_date") or ""),
            "입고 거래일수": int(row.get("normal_inbound_day_count_365") or 0),
            "평균 입고간격일": row.get("avg_inbound_cycle_days"),
            "입고 자료상태": str(row.get("inbound_data_status") or "insufficient"),
            "입고 지연후보": bool(row.get("inbound_delayed_candidate")),
            "최근입고 대표매입처코드": str(row.get("recent_inbound_vendor_code") or ""),
            "최근입고 대표매입처출처": str(row.get("recent_inbound_vendor_source") or "none"),
            "최근365일 입고이력": bool(row.get("normal_inbound_365_exists")),
        }
        detail.update({
            "최근 정상 입고일": str(row.get("last_normal_inbound_date") or ""),
            "입고 경과일": row.get("inbound_delay_days"),
            "정상 입고 거래일수": int(row.get("normal_inbound_day_count_365") or 0),
            "평균 입고간격일": row.get("avg_inbound_cycle_days"),
            "입고 자료상태": {
                "normal": "정상",
                "delayed_candidate": "지연후보",
                "insufficient": "자료부족",
            }.get(str(row.get("inbound_data_status") or ""), "자료부족"),
            "입고 지연후보": "예" if row.get("inbound_delayed_candidate") else "아니오",
            "최근입고 대표매입처코드": str(row.get("recent_inbound_vendor_code") or ""),
            "최근입고 대표매입처명": str(row.get("recent_inbound_vendor_name") or ""),
            "최근입고 대표매입처출처": {
                "actual_inbound": "실제입고",
                "master_order_vendor": "제품마스터 발주처",
                "none": "자료없음",
            }.get(str(row.get("recent_inbound_vendor_source") or ""), "자료없음"),
        })
        detail["_주요매입처필터키"] = _risk_detail_vendor_key(detail)
        detail_rows.append(detail)

    if detail_rows:
        frame = pd.DataFrame(detail_rows)
        frame["_위험상태정렬"] = frame["위험상태"].map({"긴급 부족": 1, "부족 주의": 2}).fillna(99)
        frame = frame.sort_values(
            ["_위험상태정렬", "위험보정부족예상금액", "위험보정부족예상수량", "제품코드"],
            ascending=[True, False, False, True],
            kind="stable",
        ).drop(columns=["_위험상태정렬"])
        detail_rows = frame.to_dict("records")

    detail_frame = pd.DataFrame(detail_rows)
    summary = {
        "source_rows": int(len(detail_rows)),
        "emergency_rows": int((detail_frame.get("위험상태", pd.Series(dtype="object")) == "긴급 부족").sum()),
        "warning_rows": int((detail_frame.get("위험상태", pd.Series(dtype="object")) == "부족 주의").sum()),
        "amount_positive_rows": int((pd.to_numeric(detail_frame.get("위험보정부족예상금액", pd.Series(dtype="float64")), errors="coerce").fillna(0) > 0).sum()),
        "zero_amount_rows": int((pd.to_numeric(detail_frame.get("위험보정부족예상금액", pd.Series(dtype="float64")), errors="coerce").fillna(0) <= 0).sum()),
        "surge_rows": int(detail_frame.get("수요급증여부", pd.Series(dtype="bool")).fillna(False).astype(bool).sum()),
        "assigned_vendor_rows": int((detail_frame.get("주요매입처상태", pd.Series(dtype="object")) == "assigned").sum()),
        "unassigned_vendor_rows": int((detail_frame.get("주요매입처상태", pd.Series(dtype="object")) != "assigned").sum()),
        "default_display_limit": 100,
    }
    return detail_rows, summary


def filter_dashboard_risk_detail_rows(
    rows: Any,
    *,
    risk_status: str = "전체 위험",
    vendor_key: str = "전체",
    surge_filter: str = "전체",
    include_zero_amount: bool = True,
    search_text: str = "",
) -> tuple[pd.DataFrame, dict[str, int], int]:
    """Filter already-calculated Dashboard risk rows without any source reload."""
    started = time.perf_counter()
    frame = pd.DataFrame(rows or [])
    if frame.empty:
        return frame, {"source_rows": 0, "filtered_rows": 0, "emergency_rows": 0, "warning_rows": 0, "zero_amount_rows": 0}, 0
    for column in _RISK_DETAIL_COLUMNS:
        if column not in frame.columns:
            frame[column] = 0.0 if column.endswith(("수량", "금액", "단가", "준비율")) else ""
    if "_주요매입처필터키" not in frame.columns:
        frame["_주요매입처필터키"] = frame.apply(_risk_detail_vendor_key, axis=1)

    source_rows = int(len(frame))
    if risk_status in {"긴급 부족", "부족 주의"}:
        frame = frame.loc[frame["위험상태"].eq(risk_status)]
    if vendor_key and vendor_key != "전체":
        frame = frame.loc[frame["_주요매입처필터키"].eq(vendor_key)]
    if surge_filter == "수요급증":
        frame = frame.loc[frame["수요급증여부"].fillna(False).astype(bool)]
    elif surge_filter == "일반":
        frame = frame.loc[~frame["수요급증여부"].fillna(False).astype(bool)]
    amount = pd.to_numeric(frame["위험보정부족예상금액"], errors="coerce").fillna(0)
    if not include_zero_amount:
        frame = frame.loc[amount.gt(0)]
    search = str(search_text or "").strip().casefold()
    if search:
        search_mask = (
            frame["제품코드"].fillna("").astype(str).str.casefold().str.contains(search, regex=False)
            | frame["제품명"].fillna("").astype(str).str.casefold().str.contains(search, regex=False)
        )
        frame = frame.loc[search_mask]
    frame = frame.copy()
    frame["_위험상태정렬"] = frame["위험상태"].map({"긴급 부족": 1, "부족 주의": 2}).fillna(99)
    frame = frame.sort_values(
        ["_위험상태정렬", "위험보정부족예상금액", "위험보정부족예상수량", "제품코드"],
        ascending=[True, False, False, True],
        kind="stable",
    ).drop(columns=["_위험상태정렬"], errors="ignore")
    summary = {
        "source_rows": source_rows,
        "filtered_rows": int(len(frame)),
        "emergency_rows": int(frame["위험상태"].eq("긴급 부족").sum()),
        "warning_rows": int(frame["위험상태"].eq("부족 주의").sum()),
        "zero_amount_rows": int(pd.to_numeric(frame["위험보정부족예상금액"], errors="coerce").fillna(0).le(0).sum()),
    }
    return frame, summary, int((time.perf_counter() - started) * 1000)


def _attach_dashboard_inbound_facts(
    rows: list[dict[str, Any]],
    inbound_facts_df: pd.DataFrame | None,
    *,
    inbound_source_call_count: int = 0,
) -> dict[str, Any]:
    """Left-attach read-only inbound facts without changing stock-risk policy."""
    started = time.perf_counter()
    frame = inbound_facts_df.copy() if isinstance(inbound_facts_df, pd.DataFrame) else pd.DataFrame()
    by_product: dict[str, dict[str, Any]] = {}
    if not frame.empty and "product_code" in frame.columns:
        frame["product_code"] = frame["product_code"].fillna("").astype(str).str.strip()
        for item in frame.to_dict("records"):
            code = str(item.get("product_code") or "").strip()
            if code:
                by_product[code] = item

    delayed = insufficient = fallback = no_vendor = history_365 = history_90 = 0
    for row in rows:
        inbound = by_product.get(str(row.get("product_code") or "").strip(), {})
        # Keep explicit None values for products with no history; this prevents
        # them from being mistaken for a real zero-day cycle in the detail UI.
        row.update({
            "last_normal_inbound_date": str(inbound.get("last_normal_inbound_date") or ""),
            "normal_inbound_day_count_365": int(inbound.get("normal_inbound_day_count_365") or 0),
            "avg_inbound_cycle_days": inbound.get("avg_inbound_cycle_days"),
            "inbound_delay_days": inbound.get("inbound_delay_days"),
            "inbound_delay_threshold_days": inbound.get("inbound_delay_threshold_days"),
            "inbound_data_status": str(inbound.get("inbound_data_status") or "insufficient"),
            "inbound_delayed_candidate": bool(inbound.get("inbound_delayed_candidate")),
            "normal_inbound_raw_qty_365": float(inbound.get("normal_inbound_raw_qty_365") or 0.0),
            "normal_inbound_positive_qty_365": float(inbound.get("normal_inbound_positive_qty_365") or 0.0),
            "inbound_return_raw_qty_365": float(inbound.get("inbound_return_raw_qty_365") or 0.0),
            "normal_inbound_90_exists": bool(inbound.get("normal_inbound_90_exists")),
            "normal_inbound_365_exists": bool(inbound.get("normal_inbound_365_exists")),
            "recent_inbound_vendor_code": str(inbound.get("recent_inbound_vendor_code") or ""),
            "recent_inbound_vendor_name": str(inbound.get("recent_inbound_vendor_name") or ""),
            "recent_inbound_vendor_qty_90": float(inbound.get("recent_inbound_vendor_qty_90") or 0.0),
            "recent_inbound_vendor_last_date": str(inbound.get("recent_inbound_vendor_last_date") or ""),
            "recent_inbound_vendor_source": str(inbound.get("recent_inbound_vendor_source") or "none"),
            "recent_inbound_vendor_fallback": bool(inbound.get("recent_inbound_vendor_fallback")),
        })
        status_label = {
            "normal": "정상",
            "delayed_candidate": "지연후보",
            "insufficient": "자료부족",
        }.get(row["inbound_data_status"], "자료부족")
        source_label = {
            "actual_inbound": "실제입고",
            "master_order_vendor": "제품마스터 발주처",
            "none": "자료없음",
        }.get(row["recent_inbound_vendor_source"], "자료없음")
        row.update({
            "최근 정상 입고일": row["last_normal_inbound_date"],
            "입고 경과일": row["inbound_delay_days"],
            "정상 입고 거래일수": row["normal_inbound_day_count_365"],
            "평균 입고간격일": row["avg_inbound_cycle_days"],
            "입고 자료상태": status_label,
            "입고 지연후보": "예" if row["inbound_delayed_candidate"] else "아니오",
            "최근입고 대표매입처코드": row["recent_inbound_vendor_code"],
            "최근입고 대표매입처명": row["recent_inbound_vendor_name"],
            "최근입고 대표매입처출처": source_label,
        })
        delayed += int(row["inbound_delayed_candidate"])
        insufficient += int(row["inbound_data_status"] == "insufficient")
        fallback += int(row["recent_inbound_vendor_source"] == "master_order_vendor")
        no_vendor += int(row["recent_inbound_vendor_source"] == "none")
        history_365 += int(row["normal_inbound_365_exists"])
        history_90 += int(row["normal_inbound_90_exists"])
    summary = {
        "dashboard_products": int(len(rows)),
        "history_365_products": history_365,
        "history_90_products": history_90,
        "insufficient_products": insufficient,
        "delayed_products": delayed,
        "fallback_products": fallback,
        "no_vendor_products": no_vendor,
        "cycle_days": 365,
        "vendor_days": 90,
        "inbound_source_call_count": int(inbound_source_call_count),
        "inbound_io_policy": "fixed_normal_return_whitelist",
        "attach_elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
    log.info(
        "[dashboard.inbound.facts] dashboard_products=%s history_365_products=%s history_90_products=%s insufficient_products=%s delayed_products=%s fallback_products=%s no_vendor_products=%s attach_elapsed_ms=%s",
        summary["dashboard_products"], summary["history_365_products"], summary["history_90_products"],
        summary["insufficient_products"], summary["delayed_products"], summary["fallback_products"], summary["no_vendor_products"], summary["attach_elapsed_ms"],
    )
    return summary


def _build_inventory_facts(
    payload: Mapping[str, Any] | None,
    *,
    readiness_warning_pct: float = STOCK_READY_THRESHOLD_PCT,
    evaluation_month: str = "",
    policy_date: str = "",
    demand_surge_history: Mapping[str, Any] | None = None,
    purchase_vendor_df: pd.DataFrame | None = None,
    purchase_history_month_from: str = "",
    inbound_facts_df: pd.DataFrame | None = None,
    source_call_count: int = 0,
    inbound_source_call_count: int = 0,
    stock_mode: str = "real",
    measurement: DashboardQueryMeasurement | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    phase_measurement = measurement or get_active_dashboard_query_measurement()

    def _record_inventory_phase(
        phase: str,
        started_at: float,
        *,
        input_rows: int = 0,
        result_rows: int = 0,
        input_cols: int = 0,
        result_cols: int = 0,
    ) -> None:
        if phase_measurement is None:
            return
        phase_measurement.add_phase(
            phase=phase,
            source_name="facts",
            input_rows=input_rows,
            result_rows=result_rows,
            input_cols=input_cols,
            result_cols=result_cols,
            elapsed_ms=int((time.perf_counter() - started_at) * 1000),
        )

    input_started = time.perf_counter()
    df = _payload_df(payload)
    meta = _payload_meta(payload)
    _record_inventory_phase(
        "inventory_input_prepare",
        input_started,
        result_rows=len(df),
        result_cols=len(df.columns),
    )
    rows: list[dict[str, Any]] = []
    if not df.empty:
        name_col = "제품명" if "제품명" in df.columns else ("제품코드" if "제품코드" in df.columns else df.columns[0])
        code_col = "제품코드" if "제품코드" in df.columns else name_col
        maker_cols = ["제약사명", "제조사명", "매입처명"]
        normalize_started = time.perf_counter()
        work_rows: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            product_code = str(row.get(code_col) or "").strip()
            stock = _num(row.get("현재재고수량"))
            remaining = _num(row.get("당월 잔여예상출고수량"))
            source_shortage_qty = _num(row.get("부족예상수량"), max(remaining - stock, 0))
            source_shortage_amt = _num(row.get("부족예상금액"))
            unit_price = _safe_div(source_shortage_amt, source_shortage_qty, 0.0) if source_shortage_qty > 0 else 0.0
            if not unit_price:
                unit_price = _num(row.get("재고평가단가")) or _num(row.get("평가단가")) or _num(row.get("장부재고평가단가"))
            required_values = [
                row.get("현재재고수량"),
                row.get("당월 잔여예상출고수량"),
                row.get("부족예상수량"),
                row.get("부족예상금액"),
                unit_price,
            ]
            work_rows.append(
                {
                    "product_code": product_code,
                    "product_name": str(row.get(name_col) or "").strip() or "미지정",
                    "manufacturer_name": _first_text(row, maker_cols),
                    "specification": str(row.get("규격") or "").strip(),
                    "product_group_name": str(row.get("제품그룹명") or "").strip(),
                    "product_di_name": str(row.get("제품구분명") or "").strip(),
                    "product_class_name": str(row.get("제품분류명") or "").strip(),
                    "current_stock_qty": stock,
                    "current_stock_amt": _num(row.get("현재재고금액")),
                    "remaining_expected_demand_qty": remaining,
                    "당월현재출고수량": _num(row.get("당월 현재출고수량")),
                    "당월기준예상출고수량": _num(row.get("당월 예상출고수량")),
                    "3개월필요수량": _num(row.get("3개월필요수량")),
                    "_source_shortage_qty": source_shortage_qty,
                    "_source_shortage_amt": source_shortage_amt,
                    "_unit_price": unit_price,
                    "_stock_cover_stock_present": row.get("현재재고수량") is not None and pd.notna(row.get("현재재고수량")) and str(row.get("현재재고수량")).strip() != "",
                    "_stock_cover_demand_present": row.get("당월 잔여예상출고수량") is not None and pd.notna(row.get("당월 잔여예상출고수량")) and str(row.get("당월 잔여예상출고수량")).strip() != "",
                    "_stock_risk_required_values_present": all(
                        value is not None and pd.notna(value) and str(value).strip() != ""
                        for value in required_values
                    ),
                }
            )
        _record_inventory_phase(
            "inventory_code_normalize",
            normalize_started,
            input_rows=len(df),
            result_rows=len(work_rows),
            input_cols=len(df.columns),
        )

        groupby_started = time.perf_counter()
        grouped: dict[str, dict[str, Any]] = {}
        for item in work_rows:
            key = item["product_code"] or item["product_name"] or f"__row_{len(grouped)}"
            acc = grouped.setdefault(
                key,
                {
                    "product_code": item["product_code"],
                    "product_name": item["product_name"],
                    "manufacturer_name": item["manufacturer_name"],
                    "specification": item["specification"],
                    "product_group_name": item["product_group_name"],
                    "product_di_name": item["product_di_name"],
                    "product_class_name": item["product_class_name"],
                    "current_stock_qty": 0.0,
                    "current_stock_amt": 0.0,
                    "remaining_expected_demand_qty": 0.0,
                    "당월현재출고수량": 0.0,
                    "당월기준예상출고수량": 0.0,
                    "3개월필요수량": 0.0,
                    "_source_shortage_qty": 0.0,
                    "_source_shortage_amt": 0.0,
                    "_unit_price_values": [],
                    "_stock_cover_stock_present": True,
                    "_stock_cover_demand_present": True,
                    "_stock_risk_required_values_present": True,
                },
            )
            if not acc.get("manufacturer_name") and item.get("manufacturer_name"):
                acc["manufacturer_name"] = item["manufacturer_name"]
            for field in ("specification", "product_group_name", "product_di_name", "product_class_name"):
                if not acc.get(field) and item.get(field):
                    acc[field] = str(item[field])
            acc["current_stock_qty"] += float(item.get("current_stock_qty") or 0)
            acc["current_stock_amt"] += float(item.get("current_stock_amt") or 0)
            acc["remaining_expected_demand_qty"] += float(item.get("remaining_expected_demand_qty") or 0)
            acc["당월현재출고수량"] += float(item.get("당월현재출고수량") or 0)
            acc["당월기준예상출고수량"] += float(item.get("당월기준예상출고수량") or 0)
            acc["3개월필요수량"] += float(item.get("3개월필요수량") or 0)
            acc["_source_shortage_qty"] += float(item.get("_source_shortage_qty") or 0)
            acc["_source_shortage_amt"] += float(item.get("_source_shortage_amt") or 0)
            if float(item.get("_unit_price") or 0) > 0:
                acc["_unit_price_values"].append(float(item.get("_unit_price") or 0))
            acc["_stock_risk_required_values_present"] = bool(acc["_stock_risk_required_values_present"]) and bool(item.get("_stock_risk_required_values_present"))
            acc["_stock_cover_stock_present"] = bool(acc["_stock_cover_stock_present"]) and bool(item.get("_stock_cover_stock_present"))
            acc["_stock_cover_demand_present"] = bool(acc["_stock_cover_demand_present"]) and bool(item.get("_stock_cover_demand_present"))
        _record_inventory_phase(
            "inventory_groupby",
            groupby_started,
            input_rows=len(work_rows),
            result_rows=len(grouped),
        )

        amount_started = time.perf_counter()
        for row in grouped.values():
            stock = float(row.get("current_stock_qty") or 0)
            remaining = float(row.get("remaining_expected_demand_qty") or 0)
            shortage_qty = max(remaining - max(stock, 0.0), 0.0)
            unit_price = _safe_div(
                float(row.get("_source_shortage_amt") or 0),
                float(row.get("_source_shortage_qty") or 0),
                0.0,
            )
            if not unit_price and row.get("_unit_price_values"):
                unit_price = float(row["_unit_price_values"][0])
            shortage_amt = shortage_qty * unit_price
            if remaining <= 0:
                readiness = 100.0
                status = "수요 없음/충분"
                has_demand = False
            else:
                readiness = min(max(stock, 0.0), remaining) / remaining * 100.0
                readiness = min(readiness, 100.0)
                status = "충분" if readiness >= STOCK_READY_THRESHOLD_PCT else "조치 필요"
                has_demand = True
            rows.append(
                {
                    "product_code": str(row.get("product_code") or "").strip(),
                    "product_name": str(row.get("product_name") or "").strip() or "미지정",
                    "manufacturer_name": str(row.get("manufacturer_name") or "").strip(),
                    "규격": str(row.get("specification") or "").strip(),
                    "제품그룹명": str(row.get("product_group_name") or "").strip(),
                    "제품구분명": str(row.get("product_di_name") or "").strip(),
                    "제품분류명": str(row.get("product_class_name") or "").strip(),
                    "current_stock_qty": stock,
                    "current_stock_amt": float(row.get("current_stock_amt") or 0),
                    "remaining_expected_demand_qty": remaining,
                    "당월현재출고수량": float(row.get("당월현재출고수량") or 0),
                    "당월기준예상출고수량": float(row.get("당월기준예상출고수량") or 0),
                    "3개월필요수량": float(row.get("3개월필요수량") or 0),
                    "stock_readiness_pct": readiness,
                    "shortage_qty": shortage_qty,
                    "shortage_amt": shortage_amt,
                    "stock_valuation_unit_price": unit_price,
                    "_stock_cover_stock_present": bool(row.get("_stock_cover_stock_present")),
                    "_stock_cover_demand_present": bool(row.get("_stock_cover_demand_present")),
                    "_stock_risk_required_values_present": bool(row.get("_stock_risk_required_values_present")),
                    "has_demand": has_demand,
                    "status": status,
                }
            )
        _record_inventory_phase(
            "inventory_amount_calculation",
            amount_started,
            input_rows=len(grouped),
            result_rows=len(rows),
        )

    sales_merge_started = time.perf_counter()
    demand_surge_context = _apply_current_month_demand_surge(
        rows,
        evaluation_month=evaluation_month,
        policy_date=policy_date,
    )
    demand_surge_detail = _apply_demand_surge_detail(
        rows,
        history=demand_surge_history or {},
        evaluation_month=evaluation_month,
    )
    _record_inventory_phase(
        "inventory_sales_merge",
        sales_merge_started,
        input_rows=len(rows),
        result_rows=len(rows),
    )
    risk_classification_started = time.perf_counter()
    stock_risk_summary = _classify_stock_risk_rows(
        rows,
        readiness_warning_pct=readiness_warning_pct,
    )
    _record_inventory_phase(
        "inventory_risk_classification",
        risk_classification_started,
        input_rows=len(rows),
        result_rows=len(rows),
    )
    inbound_merge_started = time.perf_counter()
    vendor_stock_risk = _attach_major_purchase_vendors(
        rows,
        purchase_vendor_df,
        evaluation_month=evaluation_month,
        history_month_from=purchase_history_month_from,
        source_call_count=source_call_count,
    )
    inbound_summary = _attach_dashboard_inbound_facts(
        rows,
        inbound_facts_df,
        inbound_source_call_count=inbound_source_call_count,
    )
    _record_inventory_phase(
        "inventory_inbound_merge",
        inbound_merge_started,
        input_rows=len(rows),
        result_rows=len(rows),
    )
    stock_merge_started = time.perf_counter()
    stock_extension_summary = _attach_stock_extension_facts(
        rows,
        evaluation_remaining_days=demand_surge_context["evaluation_remaining_days"],
    )
    _record_inventory_phase(
        "inventory_stock_merge",
        stock_merge_started,
        input_rows=len(rows),
        result_rows=len(rows),
    )
    risk_detail_started = time.perf_counter()
    risk_detail_rows, risk_detail_summary = _build_dashboard_risk_detail(rows, stock_mode=stock_mode)
    _record_inventory_phase(
        "risk_detail_rows",
        risk_detail_started,
        input_rows=len(rows),
        result_rows=len(risk_detail_rows),
    )
    rows_finalize_started = time.perf_counter()
    demand_rows = [r for r in rows if r.get("재고위험상태") != "판정 제외"]
    ready_rows = [r for r in rows if r.get("재고위험상태") == "적정"]
    shortage_rows = [r for r in rows if r.get("재고위험상태") in {"긴급 부족", "부족 주의"}]
    summary_by_status = {str(row.get("재고위험상태") or ""): row for row in stock_risk_summary}
    ready_count = int((summary_by_status.get("적정") or {}).get("품목수") or 0)
    shortage_count = sum(
        int((summary_by_status.get(status) or {}).get("품목수") or 0)
        for status in ("긴급 부족", "부족 주의")
    )
    total_demand_skus = ready_count + shortage_count
    sku_readiness_pct = _safe_div(ready_count * 100.0, total_demand_skus, 100.0)
    risk_targets = sorted(
        shortage_rows,
        key=lambda r: (
            -float(r.get("위험보정부족예상금액", r.get("shortage_amt")) or 0),
            -float(r.get("위험보정부족예상수량", r.get("shortage_qty")) or 0),
            float(r.get("위험보정재고준비율", r.get("stock_readiness_pct")) or 0),
            str(r.get("product_code") or ""),
        ),
    )[:10]
    stock_overstock_rows = [row for row in rows if bool(row.get("과잉후보여부"))]
    stock_overstock_summary = {
        "품목수": int(_stock_risk_item_identity(pd.DataFrame(stock_overstock_rows)).nunique()) if stock_overstock_rows else 0,
        "과잉후보수량": float(sum(float(row.get("과잉후보수량") or 0) for row in stock_overstock_rows)),
        "과잉후보금액": float(sum(float(row.get("과잉후보금액") or 0) for row in stock_overstock_rows)),
        "기준": "현재재고 > 3개월 필요수량",
        "포함상태": "적정",
    }
    demand_surge_rows = [row for row in rows if bool(row.get("수요급증여부"))]
    demand_surge_summary = {
        "품목수": int(_stock_risk_item_identity(pd.DataFrame(demand_surge_rows)).nunique()) if demand_surge_rows else 0,
        "전체수요급증품목수": int(_stock_risk_item_identity(pd.DataFrame(demand_surge_rows)).nunique()) if demand_surge_rows else 0,
        "기존예상초과품목수": int(demand_surge_detail["forecast_exceeded_rows"]),
        "예상외출고발생품목수": int(demand_surge_detail["unexpected_outbound_rows"]),
        "예상누락품목수": int(demand_surge_detail["forecast_omission_rows"]),
        "계절성재발생후보품목수": int(demand_surge_detail["seasonal_recurrence_candidate_rows"]),
        "3개월이상재출고품목수": int(demand_surge_detail["reactivated_after_3m_rows"]),
        "신규출고후보품목수": int(demand_surge_detail["new_outbound_candidate_rows"]),
        "분류자료부족품목수": int(demand_surge_detail["insufficient_history_rows"]),
        "이력지원시작월": demand_surge_detail["history_month_from"],
        "이력지원종료월": demand_surge_detail["history_month_to"],
        "최근판정개월수": int(demand_surge_detail["recent_month_count"]),
        "계절성판정기준": demand_surge_detail["seasonality_rule"],
        "긴급부족품목수": sum(1 for row in demand_surge_rows if row.get("재고위험상태") == "긴급 부족"),
        "부족주의품목수": sum(1 for row in demand_surge_rows if row.get("재고위험상태") == "부족 주의"),
        "적정품목수": sum(1 for row in demand_surge_rows if row.get("재고위험상태") == "적정"),
        "진행속도보정잔여수요": float(sum(float(row.get("위험보정잔여예상수요") or 0) for row in demand_surge_rows)),
        "위험보정부족예상수량": float(sum(float(row.get("위험보정부족예상수량") or 0) for row in demand_surge_rows)),
        "위험보정부족예상금액": float(sum(float(row.get("위험보정부족예상금액") or 0) for row in demand_surge_rows)),
        "평가월경과일수": demand_surge_context["evaluation_elapsed_days"],
        "평가월총일수": demand_surge_context["evaluation_total_days"],
    }
    log.info(
        "[dashboard.demand_surge_detail] inventory_rows=%s total_rows=%s forecast_exceeded_rows=%s unexpected_outbound_rows=%s forecast_omission_rows=%s seasonal_recurrence_candidate_rows=%s reactivated_after_3m_rows=%s new_outbound_candidate_rows=%s insufficient_history_rows=%s history_month_from=%s history_month_to=%s elapsed_ms=%s",
        len(rows),
        demand_surge_detail["total_rows"],
        demand_surge_detail["forecast_exceeded_rows"],
        demand_surge_detail["unexpected_outbound_rows"],
        demand_surge_detail["forecast_omission_rows"],
        demand_surge_detail["seasonal_recurrence_candidate_rows"],
        demand_surge_detail["reactivated_after_3m_rows"],
        demand_surge_detail["new_outbound_candidate_rows"],
        demand_surge_detail["insufficient_history_rows"],
        demand_surge_detail["history_month_from"],
        demand_surge_detail["history_month_to"],
        int((time.perf_counter() - started) * 1000),
    )

    _record_inventory_phase(
        "inventory_rows_finalize",
        rows_finalize_started,
        input_rows=len(rows),
        result_rows=len(rows),
    )
    return {
        "metrics": {
            "ready_sku_count": _fact(
                "재고충분 SKU 수",
                ready_count,
                unit="개",
                aggregation="count",
                grain="제품",
                time_basis="위험보정 잔여예상수요 기준 (수요급증 시 진행속도 보정, 그 외 기존 예상)",
                source_columns=["현재재고수량", "위험보정잔여예상수요", "위험보정재고준비율", "당월 잔여예상출고수량", "재고준비율"],
                comparable_with=["재고부족 SKU 수"],
            ),
            "shortage_sku_count": _fact(
                "재고부족 SKU 수",
                shortage_count,
                unit="개",
                aggregation="count",
                grain="제품",
                time_basis="위험보정 잔여예상수요 기준 (수요급증 시 진행속도 보정, 그 외 기존 예상)",
                source_columns=["현재재고수량", "위험보정잔여예상수요", "위험보정재고준비율", "당월 잔여예상출고수량", "재고준비율"],
                comparable_with=["재고충분 SKU 수"],
            ),
            "sku_readiness_pct": _fact(
                "SKU 재고준비율",
                sku_readiness_pct,
                unit="%",
                aggregation="ready_sku / demand_sku",
                grain="제품",
                time_basis="위험보정 잔여예상수요 기준 (수요급증 시 진행속도 보정, 그 외 기존 예상)",
                source_columns=["현재재고수량", "위험보정잔여예상수요", "위험보정재고준비율", "당월 잔여예상출고수량", "재고준비율"],
            ),
            "shortage_qty": _fact(
                "부족수량",
                sum(float(r.get("위험보정부족예상수량", r.get("shortage_qty")) or 0) for r in rows),
                unit="수량",
                aggregation="sum",
                grain="제품",
                time_basis="위험보정 잔여예상수요 기준 (수요급증 시 진행속도 보정, 그 외 기존 예상)",
                source_columns=["위험보정잔여예상수요", "위험보정부족예상수량", "부족예상수량"],
            ),
        },
        "readiness_rows": rows,
        "risk_targets": risk_targets,
        "stock_risk_summary": stock_risk_summary,
        "stock_overstock_summary": stock_overstock_summary,
        "stock_demand_surge_summary": demand_surge_summary,
        "vendor_stock_risk_summary": vendor_stock_risk["summary"],
        "vendor_stock_risk_rows": vendor_stock_risk["rows"],
        "vendor_stock_risk_top_rows": vendor_stock_risk["top_rows"],
        "inbound_summary": inbound_summary,
        "stock_extension_summary": stock_extension_summary,
        "risk_detail_summary": risk_detail_summary,
        "risk_detail_rows": risk_detail_rows,
        "data_quality": [] if rows else ["재고준비율 산정 자료 없음"],
    }


def _build_today_actions(sales: dict[str, Any], inventory: dict[str, Any], turnover: dict[str, Any]) -> list[dict[str, Any]]:
    """Build deterministic Dashboard actions from existing facts only."""
    started = time.perf_counter()
    candidates: list[dict[str, Any]] = []
    for row in inventory.get("readiness_rows") or inventory.get("risk_targets", []):
        product_code = str(row.get("product_code") or "").strip()
        product_name = str(row.get("product_name") or "").strip()
        risk_status = str(row.get("재고위험상태") or "").strip()
        is_shortage = risk_status in {"긴급 부족", "부족 주의"}
        is_overstock = bool(row.get("과잉후보여부"))
        if not is_shortage and not is_overstock:
            continue
        remaining = float(row.get("위험보정잔여예상수요", row.get("remaining_expected_demand_qty")) or 0)
        shortage_qty = float(row.get("위험보정부족예상수량", row.get("shortage_qty")) or 0)
        shortage_amt = float(row.get("위험보정부족예상금액", row.get("shortage_amt")) or 0)
        readiness_pct = float(row.get("위험보정재고준비율", row.get("stock_readiness_pct")) or 0)
        if is_shortage:
            demand_surge = bool(row.get("수요급증여부"))
            adjustment_basis = str(row.get("위험보정기준") or "")
            evidence = f"잔여수요 {remaining:,.0f}, 부족 {shortage_qty:,.0f}"
            if demand_surge:
                evidence = f"수요급증 · {adjustment_basis or '진행속도 보정'} · {evidence}"
            severity = 0 if risk_status == "긴급 부족" else 1
            cause_type = "stock_shortage"
            recommended_action = "발주·재고이동·대체공급 확인"
            threshold_label = "재고준비율"
            threshold_value = readiness_pct
            evidence_label = "위험보정부족예상금액"
            evidence_value = shortage_amt
            evidence_unit = "원"
        else:
            severity = 3
            cause_type = "overstock_candidate"
            evidence = str(row.get("과잉후보사유") or "현재재고가 3개월 필요수량 초과")
            evidence_label = "과잉후보금액"
            evidence_value = float(row.get("과잉후보금액") or 0)
            evidence_unit = "원"
            threshold_label = "과잉후보 기준"
            threshold_value = float(row.get("3개월필요수량") or 0)
            recommended_action = "재고이동·소진계획 확인"
        target_code = product_code
        target_name = product_name or product_code or "제품"
        candidates.append(
            {
                "action_id": f"{cause_type}:product:{target_code or product_name or 'missing'}",
                "_severity": severity,
                "_impact": float(evidence_value),
                "cause_type": cause_type,
                "status": risk_status or ("과잉 후보" if is_overstock else ""),
                "target_kind": "product",
                "target_code": target_code,
                "target_name": target_name,
                "evidence_label": evidence_label,
                "evidence_value": evidence_value,
                "evidence_unit": evidence_unit,
                "threshold_label": threshold_label,
                "threshold_value": threshold_value,
                "recommended_action": recommended_action,
                "drilldown_action": "품목별 재고부족현황" if target_code else "",
                "drilldown_params": {"product_code": target_code} if target_code else {},
                "source_dashboard_event_id": "",
                "target": target_name,
                "product_code": target_code,
                "product_name": product_name,
                "manufacturer_name": row.get("manufacturer_name") or "",
                "current_stock_qty": float(row.get("current_stock_qty") or 0),
                "remaining_expected_demand_qty": remaining,
                "shortage_qty": shortage_qty,
                "shortage_amt": shortage_amt,
                "stock_readiness_pct": readiness_pct,
                "evidence": evidence,
                "stock_cover_days": row.get("stock_cover_days"),
                "inbound_delayed_candidate": bool(row.get("inbound_delayed_candidate")),
                "last_normal_inbound_date": str(row.get("last_normal_inbound_date") or ""),
            }
        )

    for row in sales.get("decline_targets", []):
        target_name = str(row.get("target") or "").strip()
        if not target_name:
            continue
        amount = float(row.get("amount") or 0)
        growth_pct = float(row.get("growth_pct") or 0)
        candidates.append(
            {
                "action_id": f"sales_decline:manufacturer_name:{target_name}",
                "_severity": 2,
                "_impact": amount,
                "cause_type": "sales_decline",
                "status": "매출 감소",
                "target_kind": "manufacturer_name_unresolved",
                "target_code": "",
                "target_name": target_name,
                "evidence_label": "완료월 매출",
                "evidence_value": amount,
                "evidence_unit": "원",
                "threshold_label": "최근3개월증감률",
                "threshold_value": growth_pct,
                "recommended_action": "감소 원인과 거래처·품목 구성을 확인",
                "drilldown_action": "",
                "drilldown_params": {},
                "source_dashboard_event_id": "",
                "target": target_name,
                "evidence": str(row.get("reason") or "고매출 감소 판정"),
            }
        )

    deduped: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        action_id = str(candidate["action_id"])
        current = deduped.get(action_id)
        if current is None or (candidate["_severity"], -candidate["_impact"]) < (current["_severity"], -current["_impact"]):
            deduped[action_id] = candidate
    actions = sorted(
        deduped.values(),
        key=lambda row: (int(row["_severity"]), -float(row["_impact"]), str(row["target_code"]), str(row["target_name"])),
    )[:10]
    for rank, action in enumerate(actions, start=1):
        action["priority"] = rank
        action["risk_grade"] = action["status"]
        action.pop("_severity", None)
        action.pop("_impact", None)
    log.info(
        "[dashboard.actions] total_candidates=%s deduped_candidates=%s displayed_actions=%s shortage_actions=%s decline_actions=%s overstock_actions=%s data_quality_actions=0 elapsed_ms=%s",
        len(candidates), len(deduped), len(actions),
        sum(1 for item in actions if item.get("cause_type") == "stock_shortage"),
        sum(1 for item in actions if item.get("cause_type") == "sales_decline"),
        sum(1 for item in actions if item.get("cause_type") == "overstock_candidate"),
        int((time.perf_counter() - started) * 1000),
    )
    return actions


def build_dashboard_lite_facts(
    params: Mapping[str, Any] | None = None,
    *,
    manufacturer_summary_payload: Mapping[str, Any] | None = None,
    stock_shortage_payload: Mapping[str, Any] | None = None,
    inbound_facts_df: pd.DataFrame | None = None,
    sales_transaction_cycle_df: pd.DataFrame | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Build Dashboard Lite v0.1 facts from existing analytics payloads."""
    today = today or date.today()
    t0 = time.perf_counter()
    needs_sales_source = manufacturer_summary_payload is None
    needs_stock_source = stock_shortage_payload is None
    # The inbound source is an independent third source.  Offline callers that
    # intentionally avoid it must pass an explicit (possibly empty) DataFrame.
    needs_inbound_source = inbound_facts_df is None
    if needs_sales_source or needs_stock_source:
        service_params = normalize_dashboard_lite_params(params, today=today)
    else:
        try:
            service_params = normalize_dashboard_lite_params(params or default_dashboard_lite_scope(today=today), today=today)
        except Exception:
            service_params = default_dashboard_lite_scope(today=today)

    request_id = str(service_params.get("request_id") or f"dashboard-{int(t0 * 1_000_000)}")
    physical_measurement = DashboardQueryMeasurement(
        request_id=request_id,
        company_id=service_params.get("company_id") or "",
    )

    scope_phase_started = time.perf_counter()
    source_params = _dashboard_internal_source_params(service_params, today=today)
    sales_io_filter_mode, sales_io_selected_count = _dashboard_sales_io_scope_meta(service_params)
    existing_support_params = {
        **source_params,
        "month_from": source_params.get("dashboard_lite_trend_month_from"),
        "date_from": f"{source_params.get('dashboard_lite_trend_month_from')}01",
    }
    supplier_scope = apply_product_supplier_scope(service_params)
    scope_filter_active = supplier_scope_filter_active(service_params)
    period = {
        "month_from": service_params.get("month_from"),
        "month_to": service_params.get("month_to"),
        "evaluation_month": service_params.get("evaluation_month"),
        "date_from": service_params.get("date_from"),
        "date_to": service_params.get("date_to"),
        "trend_support_month_from": source_params.get("dashboard_lite_trend_month_from"),
        "basis": "조회 종료일 기준 당월은 부분월로 표시",
    }
    physical_measurement.add_phase(
        phase="dashboard_scope_prepare",
        source_name="facts",
        result_rows=0,
        elapsed_ms=int((time.perf_counter() - scope_phase_started) * 1000),
    )
    log.info(
        "[dashboard.scope] company_id=%s month_from=%s month_to=%s evaluation_month=%s elapsed_ms=%s",
        service_params.get("company_id") or "",
        service_params.get("month_from"),
        service_params.get("month_to"),
        service_params.get("evaluation_month"),
        int((time.perf_counter() - t0) * 1000),
    )
    log.info(
        "[dashboard.scope_contract] supplier_scope_mode=%s supplier_vendor_count=%s supplier_manager_count=%s sales_io_filter_mode=%s sales_io_selected_count=%s forecast_io_filter_applied=True current_stock_io_filter_applied=False",
        supplier_scope["product_supplier_scope_mode"],
        len(supplier_scope["manufacturer_codes"] or supplier_scope["order_vendor_codes"]),
        len(supplier_scope["manufacturer_manager_codes"] or supplier_scope["purchase_manager_codes"]),
        sales_io_filter_mode,
        sales_io_selected_count,
    )

    expanded_sales_source_df: pd.DataFrame | None = None
    existing_support_sales_df: pd.DataFrame | None = None
    visible_sales_df: pd.DataFrame | None = None
    source_monthly_actuals: list[dict[str, Any]] = []
    demand_surge_history: dict[str, Any] = {}
    filter_diagnostics: list[dict[str, Any]] = []
    product_filter_elapsed_ms = 0
    range_slice_elapsed_ms = 0
    history_aggregate_elapsed_ms = 0
    stock_timing = _stock_timing_meta({}, fallback_total_ms=0)
    purchase_vendor_df: pd.DataFrame | None = None
    purchase_bundle_perf: dict[str, Any] = {}
    sales_source_elapsed_ms = 0
    stock_source_elapsed_ms = 0
    inbound_source_elapsed_ms = 0
    inbound_query_elapsed_ms = 0
    inbound_build_elapsed_ms = 0
    inbound_source_rows = 0
    sales_cycle_elapsed_ms = 0
    sales_cycle_source_rows = 0
    sales_cycle_physical_query_count = 0
    sales_cycle_source_mode = "reused_daily_facts"
    inbound_cutoff_date = _dashboard_inbound_cutoff_date(service_params, today=today)
    if needs_inbound_source:
        from app.services.dashboard_inbound_facts_service import get_dashboard_inbound_facts

        inbound_started = time.perf_counter()
        with dashboard_query_measurement(
            physical_measurement,
            source="inbound",
            phase="inbound_facts",
            source_mode="detail_required",
        ):
            inbound_facts_df = get_dashboard_inbound_facts(
                dict(service_params),
                data_cutoff_date=inbound_cutoff_date,
                cycle_lookback_days=365,
                vendor_lookback_days=90,
            )
        inbound_source_elapsed_ms = int((time.perf_counter() - inbound_started) * 1000)
        inbound_source_rows = int(getattr(inbound_facts_df, "attrs", {}).get("inbound_source_rows") or 0)
        inbound_query_elapsed_ms = int(getattr(inbound_facts_df, "attrs", {}).get("inbound_query_elapsed_ms") or 0)
        inbound_build_elapsed_ms = int(getattr(inbound_facts_df, "attrs", {}).get("inbound_build_elapsed_ms") or 0)
    elif isinstance(inbound_facts_df, pd.DataFrame):
        inbound_source_rows = int(getattr(inbound_facts_df, "attrs", {}).get("inbound_source_rows") or len(inbound_facts_df))
        inbound_query_elapsed_ms = int(getattr(inbound_facts_df, "attrs", {}).get("inbound_query_elapsed_ms") or 0)
        inbound_build_elapsed_ms = int(getattr(inbound_facts_df, "attrs", {}).get("inbound_build_elapsed_ms") or 0)
    log.info(
        "[dashboard.inbound.source_load] source_rows=%s product_rows=%s source_call_count=%s data_cutoff_date=%s query_elapsed_ms=%s build_elapsed_ms=%s elapsed_ms=%s",
        inbound_source_rows,
        0 if not isinstance(inbound_facts_df, pd.DataFrame) else len(inbound_facts_df),
        int(needs_inbound_source),
        inbound_cutoff_date,
        inbound_query_elapsed_ms,
        inbound_build_elapsed_ms,
        inbound_source_elapsed_ms,
    )
    if needs_sales_source or needs_stock_source:
        from app.services.analytics_sales_trend_service import get_dashboard_sales_source_bundle

        t_source = time.perf_counter()
        with dashboard_query_measurement(
            physical_measurement,
            source="sales",
            phase="sales_bundle",
            source_mode=str(source_params.get("source_mode") or ""),
        ):
            source_bundle = get_dashboard_sales_source_bundle(dict(source_params))
        expanded_sales_source_df = source_bundle.get("sales_df")
        purchase_vendor_df = source_bundle.get("purchase_vendor_df")
        purchase_bundle_perf = dict(source_bundle.get("perf") or {})
        source_elapsed_ms = int((time.perf_counter() - t_source) * 1000)
        sales_source_elapsed_ms = source_elapsed_ms
        t_filter = time.perf_counter()
        expanded_sales_source_df = _filter_sales_source_for_dashboard(expanded_sales_source_df, service_params)
        if isinstance(purchase_vendor_df, pd.DataFrame) and isinstance(expanded_sales_source_df, pd.DataFrame) and "제품코드" in purchase_vendor_df.columns and "제품코드" in expanded_sales_source_df.columns:
            allowed_product_codes = set(expanded_sales_source_df["제품코드"].fillna("").astype(str).str.strip())
            purchase_vendor_df = purchase_vendor_df.loc[purchase_vendor_df["제품코드"].fillna("").astype(str).str.strip().isin(allowed_product_codes)].copy()
        product_filter_elapsed_ms = int((time.perf_counter() - t_filter) * 1000)
        t_ranges = time.perf_counter()
        source_months = _dashboard_source_months(expanded_sales_source_df)
        if isinstance(expanded_sales_source_df, pd.DataFrame) and source_months is not None:
            support_mask = source_months.between(
                str(source_params.get("dashboard_lite_trend_month_from") or ""),
                str(service_params.get("evaluation_month") or ""),
            )
            visible_mask = source_months.between(
                str(service_params.get("month_from") or ""),
                str(service_params.get("evaluation_month") or ""),
            )
            existing_support_sales_df = expanded_sales_source_df.loc[support_mask].copy()
            visible_sales_df = expanded_sales_source_df.loc[visible_mask].copy()
        else:
            existing_support_sales_df = expanded_sales_source_df
            visible_sales_df = expanded_sales_source_df
        range_slice_elapsed_ms = int((time.perf_counter() - t_ranges) * 1000)
        source_monthly_actuals = _monthly_sales_actuals_from_source(existing_support_sales_df)
        t_history = time.perf_counter()
        demand_surge_history = _build_demand_surge_history_by_product(
            expanded_sales_source_df,
            evaluation_month=str(service_params.get("evaluation_month") or ""),
            history_month_from=str(source_params.get("dashboard_lite_history_month_from") or ""),
            source_months=source_months,
        )
        history_aggregate_elapsed_ms = int((time.perf_counter() - t_history) * 1000)
        if isinstance(expanded_sales_source_df, pd.DataFrame):
            filter_diagnostics.extend(list(expanded_sales_source_df.attrs.get("dashboard_filter_diagnostics") or []))
        log.info(
            "[dashboard.source_load] source=shared_sales_source company_id=%s month_from=%s month_to=%s evaluation_month=%s rows=%s source_call_count=%s cache_used=%s supplier_scope_mode=%s supplier_scope_product_count=%s expanded_history_month_from=%s expanded_history_month_to=%s expanded_history_rows=%s existing_support_rows=%s visible_rows=%s sales_source_sql_ms=%s product_master_merge_ms=%s product_filter_ms=%s range_slice_ms=%s history_aggregate_ms=%s purchase_source_rows=%s purchase_source_sql_included=%s purchase_min_frame_ms=%s elapsed_ms=%s",
            service_params.get("company_id") or "",
            service_params.get("month_from"),
            service_params.get("month_to"),
            service_params.get("evaluation_month"),
            0 if expanded_sales_source_df is None else len(expanded_sales_source_df),
            1,
            False,
            supplier_scope["product_supplier_scope_mode"],
            int(expanded_sales_source_df["제품코드"].nunique()) if isinstance(expanded_sales_source_df, pd.DataFrame) and "제품코드" in expanded_sales_source_df.columns else 0,
            source_params.get("dashboard_lite_history_month_from") or "",
            service_params.get("evaluation_month") or "",
            0 if expanded_sales_source_df is None else len(expanded_sales_source_df),
            0 if existing_support_sales_df is None else len(existing_support_sales_df),
            0 if visible_sales_df is None else len(visible_sales_df),
            source_elapsed_ms,
            0,
            product_filter_elapsed_ms,
            range_slice_elapsed_ms,
            history_aggregate_elapsed_ms,
            0 if purchase_vendor_df is None else len(purchase_vendor_df),
            bool(purchase_bundle_perf.get("purchase_source_sql_included")),
            int(purchase_bundle_perf.get("purchase_min_frame_ms") or 0),
            int((time.perf_counter() - t_source) * 1000),
        )
        log.info(
            "[dashboard.phase] request_id=%s phase=sales_bundle input_rows=%s result_rows=%s physical_query_count=%s elapsed_ms=%s",
            request_id,
            int(purchase_bundle_perf.get("raw_bundle_rows") or 0),
            0 if expanded_sales_source_df is None else len(expanded_sales_source_df),
            physical_measurement.summary()["physical_query_count"],
            sales_source_elapsed_ms,
        )

    if manufacturer_summary_payload is None:
        from app.services.analytics_manufacturer_sales_trend_service import get_manufacturer_sales_trend_summary_result

        manufacturer_summary_payload = get_manufacturer_sales_trend_summary_result(
            dict(existing_support_params),
            raw_df=existing_support_sales_df,
        )
    else:
        manufacturer_summary_payload = _filter_payload_df_for_dashboard(manufacturer_summary_payload, service_params)
        payload_df = _payload_df(manufacturer_summary_payload)
        filter_diagnostics.extend(list(payload_df.attrs.get("dashboard_filter_diagnostics") or []))
    if stock_shortage_payload is None:
        from app.services.analytics_sales_trend_service import (
            get_sales_forecast_df,
            get_stock_shortage_result,
        )

        t_forecast = time.perf_counter()
        shared_sales_forecast_df = get_sales_forecast_df(
            {
                **service_params,
                "month_to": service_params.get("evaluation_month"),
                "date_to": source_params.get("date_to"),
            },
            raw_df=visible_sales_df,
        )
        forecast_elapsed_ms = int((time.perf_counter() - t_forecast) * 1000)
        t_source = time.perf_counter()
        with dashboard_query_measurement(
            physical_measurement,
            source="stock",
            phase="stock_shortage",
            source_mode=str(service_params.get("stock_mode") or ""),
        ):
            stock_shortage_payload = get_stock_shortage_result(
                {
                    **service_params,
                    "month_to": service_params.get("evaluation_month"),
                    "date_to": source_params.get("date_to"),
                },
                sales_raw_df=visible_sales_df,
                sales_forecast_df=shared_sales_forecast_df,
                product_universe_df=inbound_facts_df if scope_filter_active else None,
            )
        source_elapsed_ms = int((time.perf_counter() - t_source) * 1000)
        stock_source_elapsed_ms = source_elapsed_ms
        t_master_merge = time.perf_counter()
        stock_shortage_payload = _attach_dashboard_product_code_pairs(stock_shortage_payload, existing_support_sales_df)
        master_merge_elapsed_ms = int((time.perf_counter() - t_master_merge) * 1000)
        t_filter = time.perf_counter()
        stock_shortage_payload = _filter_payload_df_for_dashboard(stock_shortage_payload, service_params)
        filter_elapsed_ms = int((time.perf_counter() - t_filter) * 1000)
        source_df = _payload_df(stock_shortage_payload)
        stock_attrs = dict(getattr(source_df, "attrs", {}) or {})
        if isinstance(stock_shortage_payload, Mapping):
            stock_attrs.update(dict(stock_shortage_payload.get("meta") or {}))
        stock_timing = _stock_timing_meta(stock_attrs, fallback_total_ms=source_elapsed_ms)
        filter_diagnostics.extend(list(source_df.attrs.get("dashboard_filter_diagnostics") or []))
        log.info(
            "[dashboard.source_load] source=stock company_id=%s month_from=%s month_to=%s evaluation_month=%s rows=%s source_call_count=%s cache_used=%s sales_forecast_calc_ms=%s sales_source_reused=%s stock_sql_ms=%s stock_batch_count=%s stock_aggregate_ms=%s stock_shortage_build_ms=%s stock_shortage_total_ms=%s configured_batch_size=%s effective_chunk_size=%s fixed_parameter_count=%s stock_cd_parameter_count=%s io_gu_parameter_count=%s total_parameter_count=%s master_merge_elapsed_ms=%s product_filter_ms=%s elapsed_ms=%s",
            service_params.get("company_id") or "",
            service_params.get("month_from"),
            service_params.get("month_to"),
            service_params.get("evaluation_month"),
            len(source_df),
            1,
            False,
            forecast_elapsed_ms,
            True,
            stock_timing["stock_sql_ms"],
            stock_timing["stock_batch_count"],
            stock_timing["stock_aggregate_ms"],
            stock_timing["stock_shortage_build_ms"],
            stock_timing["stock_shortage_total_ms"],
            stock_timing["configured_batch_size"],
            stock_timing["effective_chunk_size"],
            stock_timing["fixed_parameter_count"],
            stock_timing["stock_cd_parameter_count"],
            stock_timing["io_gu_parameter_count"],
            stock_timing["total_parameter_count"],
            master_merge_elapsed_ms,
            filter_elapsed_ms,
            int((time.perf_counter() - t_source) * 1000),
        )
    else:
        stock_shortage_payload = _attach_dashboard_product_code_pairs(stock_shortage_payload, existing_support_sales_df)
        stock_shortage_payload = _filter_payload_df_for_dashboard(stock_shortage_payload, service_params)
        source_df = _payload_df(stock_shortage_payload)
        filter_diagnostics.extend(list(source_df.attrs.get("dashboard_filter_diagnostics") or []))

    t_sales = time.perf_counter()
    sales = _build_sales_facts(
        manufacturer_summary_payload,
        history_actuals=source_monthly_actuals,
        evaluation_month=service_params.get("evaluation_month"),
        policy_date=service_params.get("policy_date"),
        today=today,
    )
    log.info(
        "[dashboard.sales_facts] company_id=%s month_from=%s month_to=%s evaluation_month=%s result_rows=%s elapsed_ms=%s",
        service_params.get("company_id") or "",
        service_params.get("month_from"),
        service_params.get("month_to"),
        service_params.get("evaluation_month"),
        len(sales.get("chart_rows") or []),
        int((time.perf_counter() - t_sales) * 1000),
    )
    physical_measurement.add_phase(
        phase="sales_facts",
        source_name="sales",
        source_mode=str(source_params.get("source_mode") or ""),
        input_rows=len(_payload_df(manufacturer_summary_payload)),
        result_rows=len(sales.get("chart_rows") or []),
        elapsed_ms=int((time.perf_counter() - t_sales) * 1000),
    )
    t_stock = time.perf_counter()
    inventory = _build_inventory_facts(
        stock_shortage_payload,
        readiness_warning_pct=float(service_params.get("readiness_warning_pct") or STOCK_READY_THRESHOLD_PCT),
        evaluation_month=str(service_params.get("evaluation_month") or ""),
        policy_date=str(service_params.get("policy_date") or ""),
        demand_surge_history=demand_surge_history,
        purchase_vendor_df=purchase_vendor_df,
        purchase_history_month_from=str(source_params.get("dashboard_lite_history_month_from") or ""),
        inbound_facts_df=inbound_facts_df,
        source_call_count=int(needs_sales_source) + int(needs_stock_source),
        inbound_source_call_count=int(needs_inbound_source),
        stock_mode=str(service_params.get("stock_mode") or "real"),
        measurement=physical_measurement,
    )
    def _product_codes(frame: Any, *columns: str) -> set[str]:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return set()
        for column in columns:
            if column in frame.columns:
                return {
                    str(value).strip()
                    for value in frame[column].fillna("").astype(str).tolist()
                    if str(value).strip()
                }
        return set()

    master_product_codes = _product_codes(inbound_facts_df, "product_code")
    sales_product_codes = _product_codes(visible_sales_df, "제품코드")
    stock_product_codes = _product_codes(source_df, "제품코드")
    inbound_product_codes = _product_codes(inbound_facts_df, "product_code")
    dashboard_product_codes = {
        str(row.get("product_code") or "").strip()
        for row in (inventory.get("readiness_rows") or [])
        if str(row.get("product_code") or "").strip()
    }
    log.info(
        "[dashboard.product_universe] supplier_scope_mode=%s supplier_scope_filter_active=%s master_universe_applied=%s master_product_count=%s sales_product_count=%s stock_product_count=%s inbound_product_count=%s dashboard_product_count=%s sales_empty_product_count=%s",
        supplier_scope["product_supplier_scope_mode"],
        scope_filter_active,
        scope_filter_active,
        len(master_product_codes),
        len(sales_product_codes),
        len(stock_product_codes),
        len(inbound_product_codes),
        len(dashboard_product_codes),
        len(master_product_codes - sales_product_codes),
    )
    log.info(
        "[dashboard.stock_facts] company_id=%s month_from=%s month_to=%s evaluation_month=%s result_rows=%s elapsed_ms=%s",
        service_params.get("company_id") or "",
        service_params.get("month_from"),
        service_params.get("month_to"),
        service_params.get("evaluation_month"),
        len(inventory.get("readiness_rows") or []),
        int((time.perf_counter() - t_stock) * 1000),
    )
    purchase_cycle = {}
    if isinstance(inbound_facts_df, pd.DataFrame):
        purchase_cycle = dict(inbound_facts_df.attrs.get("purchase_transaction_cycle") or {})
    from app.services.dashboard_inbound_facts_service import summarize_transaction_cycle_dates

    sales_cycle_cutoff_date = _dashboard_inbound_cutoff_date(service_params, today=today)
    sales_cycle_source_started = time.perf_counter()
    if sales_transaction_cycle_df is None:
        from app.services.analytics_sales_trend_service import get_dashboard_sales_transaction_dates

        sales_cycle_source_mode = "rddbc120_minimal"
        try:
            with dashboard_query_measurement(
                physical_measurement,
                source="sales",
                phase="sales_transaction_cycle_source",
                source_mode=sales_cycle_source_mode,
            ):
                sales_transaction_cycle_df = get_dashboard_sales_transaction_dates(
                    dict(source_params),
                    date_from=(pd.Timestamp(sales_cycle_cutoff_date) - pd.Timedelta(days=89)).strftime("%Y%m%d"),
                    date_to=sales_cycle_cutoff_date,
                )
            sales_cycle_physical_query_count = 1
        except Exception as exc:
            sales_transaction_cycle_df = pd.DataFrame(columns=["trade_date", "io_code"])
            sales_cycle_source_mode = "error"
            log.warning(
                "[dashboard.sales_transaction_cycle] status=error error_type=%s elapsed_ms=%s",
                type(exc).__name__,
                int((time.perf_counter() - sales_cycle_source_started) * 1000),
            )
    elif isinstance(sales_transaction_cycle_df, pd.DataFrame):
        sales_cycle_source_mode = str(sales_transaction_cycle_df.attrs.get("source_mode") or sales_cycle_source_mode)
        sales_cycle_physical_query_count = int(sales_transaction_cycle_df.attrs.get("physical_query_count") or 0)

    sales_cycle_source_finished = time.perf_counter()
    sales_cycle_calculation_started = sales_cycle_source_finished
    cycle_source = sales_transaction_cycle_df if isinstance(sales_transaction_cycle_df, pd.DataFrame) else pd.DataFrame()
    sales_cycle_source_rows = int(len(cycle_source))
    normal_sale_dates = pd.Series(dtype="object")
    if not cycle_source.empty and {"trade_date", "io_code"}.issubset(cycle_source.columns):
        # Existing sales facts use 5xx for sales and 6xx for returns.  A return
        # on a normal-sale date therefore cannot erase that date.
        normal_sale_dates = cycle_source.loc[
            cycle_source["io_code"].fillna("").astype(str).str.strip().str.startswith("5"),
            "trade_date",
        ]
    sales_cycle = summarize_transaction_cycle_dates(
        normal_sale_dates,
        cutoff_date=sales_cycle_cutoff_date,
        period_days=90,
    )
    if sales_cycle_source_mode == "error":
        sales_cycle["result_status"] = "error"
    sales_cycle_calculation_finished = time.perf_counter()
    sales_cycle_timing = transaction_cycle_phase_timing(
        source_started_at=sales_cycle_source_started,
        source_finished_at=sales_cycle_source_finished,
        calculation_started_at=sales_cycle_calculation_started,
        calculation_finished_at=sales_cycle_calculation_finished,
    )
    sales_cycle_source_elapsed_ms = sales_cycle_timing["source_elapsed_ms"]
    sales_cycle_calculation_elapsed_ms = sales_cycle_timing["calculation_elapsed_ms"]
    sales_cycle_total_elapsed_ms = sales_cycle_timing["total_elapsed_ms"]
    sales_cycle.update(
        {
            "window_days": sales_cycle["period_days"],
            "latest_normal_trade_date": sales_cycle["latest_date"] or None,
            "source_mode": sales_cycle_source_mode,
            "physical_query_count": sales_cycle_physical_query_count,
            "source_rows": sales_cycle_source_rows,
            "source_elapsed_ms": sales_cycle_source_elapsed_ms,
            "calculation_elapsed_ms": sales_cycle_calculation_elapsed_ms,
            "total_elapsed_ms": sales_cycle_total_elapsed_ms,
            "elapsed_ms": sales_cycle_total_elapsed_ms,
        }
    )
    physical_measurement.add_phase(
        phase="sales_transaction_cycle_calculation",
        source_name="sales",
        input_rows=sales_cycle_source_rows,
        result_rows=int(sales_cycle.get("unique_trade_days") or 0),
        elapsed_ms=sales_cycle_calculation_elapsed_ms,
    )
    log.info(
        "[dashboard.sales_transaction_cycle] status=%s source_mode=%s window_start=%s window_end=%s source_rows=%s unique_trade_days=%s physical_query_count=%s source_elapsed_ms=%s calculation_elapsed_ms=%s total_elapsed_ms=%s",
        sales_cycle["result_status"], sales_cycle_source_mode, sales_cycle["window_start"], sales_cycle["window_end"],
        sales_cycle_source_rows, sales_cycle["unique_trade_days"], sales_cycle_physical_query_count,
        sales_cycle_source_elapsed_ms, sales_cycle_calculation_elapsed_ms, sales_cycle_total_elapsed_ms,
    )
    purchase_cycle_status = purchase_cycle.get("result_status") or purchase_cycle.get("data_status")
    sales_cycle_status = sales_cycle.get("result_status")
    turnover = {
        "status": resolve_transaction_cycle_status(purchase_cycle_status, sales_cycle_status),
        "period_days": int(purchase_cycle.get("period_days") or 90),
        "cutoff_date": str(purchase_cycle.get("cutoff_date") or ""),
        "purchase_latest_date": str(purchase_cycle.get("latest_date") or ""),
        "purchase_elapsed_days": purchase_cycle.get("elapsed_days"),
        "purchase_unique_trade_days": purchase_cycle.get("unique_trade_days"),
        "purchase_average_interval_days": purchase_cycle.get("average_interval_days"),
        "purchase_data_status": str(purchase_cycle.get("data_status") or "missing"),
        "sales_latest_date": str(sales_cycle.get("latest_date") or ""),
        "sales_elapsed_days": sales_cycle.get("elapsed_days"),
        "sales_unique_trade_days": sales_cycle.get("unique_trade_days"),
        "sales_average_interval_days": sales_cycle.get("average_interval_days"),
        "sales_data_status": str(sales_cycle_status or "no_data"),
        "sales_transaction_cycle": sales_cycle,
        "purchase_turnover_days": purchase_cycle.get("average_interval_days"),
        "sales_turnover_days": sales_cycle.get("average_interval_days"),
        "definition": "최근 90일 정상 매입·매출 고유 거래일 사이 평균 일수",
        "data_quality": [],
    }
    t_actions = time.perf_counter()
    today_actions = _build_today_actions(sales, inventory, turnover)
    actions_elapsed_ms = int((time.perf_counter() - t_actions) * 1000)
    physical_measurement.add_phase(
        phase="today_actions",
        source_name="facts",
        input_rows=len(inventory.get("readiness_rows") or []),
        result_rows=len(today_actions),
        elapsed_ms=actions_elapsed_ms,
    )
    t_visual_phase2 = time.perf_counter()
    visual_phase2_summary, purchase_trend_rows = _build_visual_phase2_summary(
        list(inventory.get("readiness_rows") or []),
        purchase_vendor_df,
        evaluation_remaining_days=next(
            (
                row.get("평가월잔여일수")
                for row in (inventory.get("readiness_rows") or [])
                if isinstance(row, Mapping) and row.get("평가월잔여일수") is not None
            ),
            0,
        ),
        today_action_count=len(today_actions),
    )
    stock_status_summary = {
        str(row.get("상태") or ""): int(row.get("품목수") or 0)
        for row in (inventory.get("stock_risk_summary") or [])
        if isinstance(row, Mapping)
    }
    visual_phase2_summary["briefing_lines"] = _build_visual_phase2_briefing(
        visual_phase2_summary,
        emergency_count=stock_status_summary.get("긴급 부족", 0),
        warning_count=stock_status_summary.get("부족 주의", 0),
    )
    visual_phase2_summary["vendor_top_count"] = min(10, len(inventory.get("vendor_stock_risk_top_rows") or []))
    inventory["visual_phase2_summary"] = visual_phase2_summary
    inventory["purchase_trend_rows"] = purchase_trend_rows
    visual_phase2_elapsed_ms = int((time.perf_counter() - t_visual_phase2) * 1000)
    log.info(
        "[dashboard.visual_phase2] inventory_rows=%s cover_zero_stock_rows=%s cover_shortfall_rows=%s cover_sufficient_rows=%s cover_no_demand_rows=%s cover_insufficient_rows=%s inbound_delay_candidate_rows=%s overstock_candidate_rows=%s recent_purchase_none_rows=%s demand_surge_rows=%s purchase_trend_status=%s purchase_trend_points=%s vendor_top_count=%s briefing_line_count=%s build_elapsed_ms=%s additional_source_call_count=0",
        visual_phase2_summary["inventory_count"], visual_phase2_summary["cover_zero_stock_count"], visual_phase2_summary["cover_shortfall_count"], visual_phase2_summary["cover_sufficient_count"], visual_phase2_summary["cover_no_demand_count"], visual_phase2_summary["cover_insufficient_count"], visual_phase2_summary["inbound_delay_candidate_count"], visual_phase2_summary["overstock_candidate_count"], visual_phase2_summary["recent_purchase_none_count"], visual_phase2_summary["demand_surge_count"], visual_phase2_summary["purchase_trend_status"], visual_phase2_summary["purchase_trend_points"], visual_phase2_summary["vendor_top_count"], len(visual_phase2_summary["briefing_lines"]), visual_phase2_summary["elapsed_ms"],
    )
    additional_notes = {
        "sales_decline_targets": sales.get("decline_targets", []),
        "turnover_status": turnover.get("status") or "",
        "data_quality": sales.get("data_quality", []) + inventory.get("data_quality", []) + turnover.get("data_quality", []) + filter_diagnostics,
        "performance": stock_timing,
        "comparison_notes": [
            "제약사별 매출 감소, 거래 회전일 자료부족, 일반 데이터 품질 안내는 제품 조치 표와 분리",
        ],
    }
    log.info(
        "[dashboard.actions] company_id=%s month_from=%s month_to=%s evaluation_month=%s result_rows=%s elapsed_ms=%s",
        service_params.get("company_id") or "",
        service_params.get("month_from"),
        service_params.get("month_to"),
        service_params.get("evaluation_month"),
        len(today_actions),
        actions_elapsed_ms,
    )
    physical_measurement.add_phase(
        phase="visual_phase2",
        source_name="facts",
        input_rows=len(inventory.get("readiness_rows") or []),
        result_rows=len(purchase_trend_rows or []),
        elapsed_ms=visual_phase2_elapsed_ms,
    )
    physical_query_summary = physical_measurement.summary()
    log.info(
        "[dashboard.phase] request_id=%s phase=facts_assembly input_rows=%s result_rows=%s physical_query_count=%s elapsed_ms=%s",
        request_id,
        len(source_df) if isinstance(source_df, pd.DataFrame) else 0,
        len(inventory.get("risk_detail_rows") or []),
        physical_query_summary["physical_query_count"],
        int((time.perf_counter() - t_stock) * 1000),
    )

    facts_payload_started = time.perf_counter()
    facts = {
        "kind": FACTS_KIND,
        "scope": "Dashboard Lite v0.1",
        "period": period,
        "filters": _dashboard_filter_facts(service_params),
        "partial_period": {
            "current_month_is_partial": True,
            "policy": "완료월 누계/평균과 당월 부분월 현재값은 직접 달성 판단으로 비교하지 않음",
        },
        "sales": sales,
        "purchase": {
            "status": "자료부족",
            "turnover_definition": "최근 90일 정상 매입 고유 입고일 사이 평균 일수",
            "returns_policy": "반품 제외, 별도 표시",
        },
        "inventory": inventory,
        "inbound_summary": dict(inventory.get("inbound_summary") or {}),
        "stock_readiness": {
            "threshold_pct": float(service_params.get("readiness_warning_pct") or STOCK_READY_THRESHOLD_PCT),
            "policy": "제품별 준비가능수량=min(max(현재재고,0), 위험보정 잔여예상수요)",
            "adjustment_policy": "수요급증 품목은 현재 출고속도 기준 진행속도 보정 월말예상을 사용하고, 일반 품목은 기존 당월 예상수요를 사용",
            "sku_readiness_pct": inventory["metrics"]["sku_readiness_pct"],
        },
        "turnover_days": turnover,
        "transaction_cycle": turnover,
        "rankings": {
            "high_sales_decline": sales.get("decline_targets", []),
            "stock_risk": inventory.get("risk_targets", []),
        },
        "trend_counts": sales.get("trend_counts", {}),
        "trend_amounts": sales.get("trend_amounts", {}),
        "trend_shares": sales.get("trend_shares", {}),
        "risk_targets": inventory.get("risk_targets", []) + sales.get("decline_targets", []),
        "today_actions": today_actions,
        "additional_notes": additional_notes,
        "performance": stock_timing,
        "data_quality": sales.get("data_quality", []) + inventory.get("data_quality", []) + turnover.get("data_quality", []) + filter_diagnostics,
        "comparison_rules": [
            "완료월 평균매출은 완료월끼리 비교",
            "당월 현재매출은 당월 예상매출 또는 진척률과 함께 해석",
            "재고준비율은 SKU 기준으로 표시하며 서로 다른 제품 수량을 전사 수량 준비율로 합산하지 않음",
        ],
        "forbidden_comparisons": [
            "완료월 총매출과 당월 부분월 현재매출의 직접 우열 판단",
            "업체 수 기준 증가/감소를 매출액 전체 증가/감소로 단정",
            "sample_records 또는 화면 일부 행으로 전체 순위/총합 판단",
            "재고 98% 이상 SKU를 기본 조치 목록에 반복 노출",
        ],
    }
    facts["base_source_call_count"] = int(needs_sales_source) + int(needs_stock_source)
    facts["inbound_source_call_count"] = int(needs_inbound_source)
    facts["source_call_count"] = facts["base_source_call_count"] + facts["inbound_source_call_count"]
    facts["performance"]["inbound_source_elapsed_ms"] = inbound_source_elapsed_ms
    facts["performance"]["inbound_query_elapsed_ms"] = inbound_query_elapsed_ms
    facts["performance"]["inbound_build_elapsed_ms"] = inbound_build_elapsed_ms
    facts["performance"]["inbound_source_rows"] = inbound_source_rows
    facts["performance"]["sales_source_elapsed_ms"] = sales_source_elapsed_ms
    facts["performance"]["stock_source_elapsed_ms"] = stock_source_elapsed_ms
    facts["performance"]["sales_transaction_cycle_elapsed_ms"] = sales_cycle_total_elapsed_ms
    facts["performance"]["sales_transaction_cycle_source_elapsed_ms"] = sales_cycle_source_elapsed_ms
    facts["performance"]["sales_transaction_cycle_calculation_elapsed_ms"] = sales_cycle_calculation_elapsed_ms
    facts["performance"]["sales_transaction_cycle_total_elapsed_ms"] = sales_cycle_total_elapsed_ms
    facts["performance"]["sales_transaction_cycle_source_rows"] = sales_cycle_source_rows
    facts["performance"]["sales_transaction_cycle_physical_query_count"] = sales_cycle_physical_query_count
    facts["performance"].update(physical_query_summary)
    physical_measurement.add_phase(
        phase="facts_payload_build",
        source_name="facts",
        result_rows=len(inventory.get("risk_detail_rows") or []),
        elapsed_ms=int((time.perf_counter() - facts_payload_started) * 1000),
    )
    total_elapsed_ms = int((time.perf_counter() - t0) * 1000)
    measurement_summary = physical_measurement.summary()
    measured_phase_total_ms = sum(int(item.get("elapsed_ms") or 0) for item in measurement_summary["phase_metrics"])
    facts["performance"].update(measurement_summary)
    facts["performance"]["post_process_elapsed_ms"] = sum(
        int(item.get("elapsed_ms") or 0)
        for item in measurement_summary.get("phase_metrics") or []
        if item.get("source_name") == "facts"
    )
    facts["performance"].update(
        {
            "total_elapsed_ms": total_elapsed_ms,
            "measured_phase_total_ms": measured_phase_total_ms,
            "unaccounted_elapsed_ms": max(0, total_elapsed_ms - measured_phase_total_ms),
            "unaccounted_ratio_pct": round(max(0, total_elapsed_ms - measured_phase_total_ms) * 100.0 / max(1, total_elapsed_ms), 2),
        }
    )
    log.info(
        "[dashboard.finish] request_id=%s company_id=%s month_from=%s month_to=%s evaluation_month=%s base_source_call_count=%s inbound_source_call_count=%s source_call_count=%s logical_source_count=%s physical_query_count=%s physical_query_count_by_source=%s sales_elapsed_ms=%s stock_elapsed_ms=%s inbound_elapsed_ms=%s stock_and_facts_elapsed_ms=%s dashboard_total_ms=%s elapsed_ms=%s",
        request_id,
        service_params.get("company_id") or "",
        service_params.get("month_from"),
        service_params.get("month_to"),
        service_params.get("evaluation_month"),
        facts["base_source_call_count"],
        facts["inbound_source_call_count"],
        facts["source_call_count"],
        physical_query_summary["logical_source_count"],
        physical_query_summary["physical_query_count"],
        physical_query_summary["physical_query_count_by_source"],
        sales_source_elapsed_ms,
        stock_source_elapsed_ms,
        inbound_source_elapsed_ms,
        int((time.perf_counter() - t_stock) * 1000),
        total_elapsed_ms,
        total_elapsed_ms,
    )
    return facts

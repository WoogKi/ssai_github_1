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
from datetime import date
import logging
import re
import time
from typing import Any, Mapping

import pandas as pd


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
    return out


def _dashboard_internal_source_params(params: Mapping[str, Any], *, today: date) -> dict[str, Any]:
    """Expand only the raw source range needed for Dashboard trend history."""
    display_from = str(params.get("month_from") or "")
    display_to = str(params.get("month_to") or "")
    evaluation_month = str(params.get("evaluation_month") or "")
    trend_month_from = _add_months(display_from, -3)
    today_ym = f"{today.year:04d}{today.month:02d}"
    source = dict(params)
    manufacturer_codes = _clean_list_param(params.get("manufacturer_test_codes"))
    source.update(
        {
            "month_from": trend_month_from,
            "month_to": evaluation_month,
            "date_from": f"{trend_month_from}01",
            "date_to": today.strftime("%Y%m%d") if evaluation_month == today_ym else _last_day_yyyymm(evaluation_month),
            "dashboard_lite_display_month_from": display_from,
            "dashboard_lite_display_month_to": display_to,
            "dashboard_lite_trend_month_from": trend_month_from,
            # Dashboard uses the explicit Gcode:Tcode keys below.  Do not let
            # legacy single-code filters reinterpret Tax classification as Gu.
            "product_di_list": [],
            "product_class_list": [],
            # This one-session test condition is resolved to product codes before
            # the shared sales and stock SQL paths execute. It is never persisted.
            "dashboard_manufacturer_codes": manufacturer_codes,
        }
    )
    return source


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
    out = df.copy()
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
    keep_mask = pd.Series(True, index=out.index)
    exclude_mask = pd.Series(False, index=out.index)
    diagnostics: list[dict[str, Any]] = []
    for spec in inclusion_specs:
        spec_mask, diag = _dashboard_filter_mask(out, **spec)
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
    if use_inclusion and not bool(keep_mask.all()):
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
    manufacturer_test_codes = _clean_list_param(params.get("manufacturer_test_codes"))
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
        # This is deliberately kept out of the shared saved profile. It is a
        # one-run test scope but still belongs in facts provenance.
        "manufacturer_test_codes": manufacturer_test_codes,
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
) -> dict[str, Any]:
    df = _payload_df(payload)
    meta = _payload_meta(payload)
    completed_total = _sum_col(df, "완료월총매출") or _num(meta.get("sum_completed_month_sales_amt"))
    completed_months = int(max([_num(v, 0) for v in df["완료월수"].tolist()], default=0)) if "완료월수" in df.columns else int(_num(meta.get("completed_month_count")))
    completed_avg = _safe_div(completed_total, completed_months)
    current_sales = _sum_col(df, "당월 현재매출") or _num(meta.get("sum_current_month_sales_amt"))
    forecast_sales = _sum_col(df, "당월 예상매출") or _num(meta.get("sum_current_month_expected_sales_amt"))
    progress_pct = _safe_div(current_sales * 100.0, forecast_sales)

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
        "trend_counts": trend_counts,
        "trend_amounts": trend_amounts,
        "trend_shares": trend_shares,
        "decline_targets": decline_targets,
        "data_quality": [] if not df.empty else ["제약사별 매출 추세 요약 자료 없음"],
    }


def _build_inventory_facts(
    payload: Mapping[str, Any] | None,
    *,
    readiness_warning_pct: float = STOCK_READY_THRESHOLD_PCT,
    evaluation_month: str = "",
    policy_date: str = "",
) -> dict[str, Any]:
    df = _payload_df(payload)
    meta = _payload_meta(payload)
    rows: list[dict[str, Any]] = []
    if not df.empty:
        name_col = "제품명" if "제품명" in df.columns else ("제품코드" if "제품코드" in df.columns else df.columns[0])
        code_col = "제품코드" if "제품코드" in df.columns else name_col
        maker_cols = ["제약사명", "제조사명", "매입처명"]
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
                    "current_stock_qty": stock,
                    "current_stock_amt": _num(row.get("현재재고금액")),
                    "remaining_expected_demand_qty": remaining,
                    "당월현재출고수량": _num(row.get("당월 현재출고수량")),
                    "당월기준예상출고수량": _num(row.get("당월 예상출고수량")),
                    "3개월필요수량": _num(row.get("3개월필요수량")),
                    "_source_shortage_qty": source_shortage_qty,
                    "_source_shortage_amt": source_shortage_amt,
                    "_unit_price": unit_price,
                    "_stock_risk_required_values_present": all(
                        value is not None and pd.notna(value) and str(value).strip() != ""
                        for value in required_values
                    ),
                }
            )

        grouped: dict[str, dict[str, Any]] = {}
        for item in work_rows:
            key = item["product_code"] or item["product_name"] or f"__row_{len(grouped)}"
            acc = grouped.setdefault(
                key,
                {
                    "product_code": item["product_code"],
                    "product_name": item["product_name"],
                    "manufacturer_name": item["manufacturer_name"],
                    "current_stock_qty": 0.0,
                    "current_stock_amt": 0.0,
                    "remaining_expected_demand_qty": 0.0,
                    "당월현재출고수량": 0.0,
                    "당월기준예상출고수량": 0.0,
                    "3개월필요수량": 0.0,
                    "_source_shortage_qty": 0.0,
                    "_source_shortage_amt": 0.0,
                    "_unit_price_values": [],
                    "_stock_risk_required_values_present": True,
                },
            )
            if not acc.get("manufacturer_name") and item.get("manufacturer_name"):
                acc["manufacturer_name"] = item["manufacturer_name"]
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
                    "_stock_risk_required_values_present": bool(row.get("_stock_risk_required_values_present")),
                    "has_demand": has_demand,
                    "status": status,
                }
            )

    demand_surge_context = _apply_current_month_demand_surge(
        rows,
        evaluation_month=evaluation_month,
        policy_date=policy_date,
    )
    stock_risk_summary = _classify_stock_risk_rows(
        rows,
        readiness_warning_pct=readiness_warning_pct,
    )
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
        "긴급부족품목수": sum(1 for row in demand_surge_rows if row.get("재고위험상태") == "긴급 부족"),
        "부족주의품목수": sum(1 for row in demand_surge_rows if row.get("재고위험상태") == "부족 주의"),
        "적정품목수": sum(1 for row in demand_surge_rows if row.get("재고위험상태") == "적정"),
        "진행속도보정잔여수요": float(sum(float(row.get("위험보정잔여예상수요") or 0) for row in demand_surge_rows)),
        "위험보정부족예상수량": float(sum(float(row.get("위험보정부족예상수량") or 0) for row in demand_surge_rows)),
        "위험보정부족예상금액": float(sum(float(row.get("위험보정부족예상금액") or 0) for row in demand_surge_rows)),
        "평가월경과일수": demand_surge_context["evaluation_elapsed_days"],
        "평가월총일수": demand_surge_context["evaluation_total_days"],
    }

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
        "data_quality": [] if rows else ["재고준비율 산정 자료 없음"],
    }


def _build_today_actions(sales: dict[str, Any], inventory: dict[str, Any], turnover: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen_products: set[str] = set()
    for row in inventory.get("risk_targets", []):
        product_code = str(row.get("product_code") or "").strip()
        dedupe_key = product_code or str(row.get("product_name") or "").strip()
        if not dedupe_key or dedupe_key in seen_products:
            continue
        seen_products.add(dedupe_key)
        remaining = float(row.get("위험보정잔여예상수요", row.get("remaining_expected_demand_qty")) or 0)
        shortage_qty = float(row.get("위험보정부족예상수량", row.get("shortage_qty")) or 0)
        shortage_amt = float(row.get("위험보정부족예상금액", row.get("shortage_amt")) or 0)
        readiness_pct = float(row.get("위험보정재고준비율", row.get("stock_readiness_pct")) or 0)
        demand_surge = bool(row.get("수요급증여부"))
        adjustment_basis = str(row.get("위험보정기준") or "")
        evidence = f"잔여수요 {remaining:,.0f}, 부족 {shortage_qty:,.0f}"
        if demand_surge:
            evidence = f"수요급증 · {adjustment_basis or '진행속도 보정'} · {evidence}"
        actions.append(
            {
                "rank": len(actions) + 1,
                "priority": "높음",
                "risk_grade": "조치 필요",
                "target": row.get("product_name") or row.get("product_code") or "제품",
                "product_code": product_code,
                "product_name": row.get("product_name") or "",
                "manufacturer_name": row.get("manufacturer_name") or "",
                "current_stock_qty": float(row.get("current_stock_qty") or 0),
                "remaining_expected_demand_qty": remaining,
                "shortage_qty": shortage_qty,
                "shortage_amt": shortage_amt,
                "stock_readiness_pct": readiness_pct,
                "status": f"준비율 {readiness_pct:.1f}%",
                "evidence": evidence,
                "recommended_action": "발주·재고이동·대체공급 확인",
                "drill_down": "품목별 재고부족현황",
            }
        )
        if len(actions) >= 10:
            break
    return actions[:10]


def build_dashboard_lite_facts(
    params: Mapping[str, Any] | None = None,
    *,
    manufacturer_summary_payload: Mapping[str, Any] | None = None,
    stock_shortage_payload: Mapping[str, Any] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Build Dashboard Lite v0.1 facts from existing analytics payloads."""
    today = today or date.today()
    t0 = time.perf_counter()
    needs_sales_source = manufacturer_summary_payload is None
    needs_stock_source = stock_shortage_payload is None
    if needs_sales_source or needs_stock_source:
        service_params = normalize_dashboard_lite_params(params, today=today)
    else:
        try:
            service_params = normalize_dashboard_lite_params(params or default_dashboard_lite_scope(today=today), today=today)
        except Exception:
            service_params = default_dashboard_lite_scope(today=today)

    source_params = _dashboard_internal_source_params(service_params, today=today)
    manufacturer_test_codes = _clean_list_param(service_params.get("manufacturer_test_codes"))
    period = {
        "month_from": service_params.get("month_from"),
        "month_to": service_params.get("month_to"),
        "evaluation_month": service_params.get("evaluation_month"),
        "date_from": service_params.get("date_from"),
        "date_to": service_params.get("date_to"),
        "trend_support_month_from": source_params.get("dashboard_lite_trend_month_from"),
        "basis": "조회 종료일 기준 당월은 부분월로 표시",
    }
    log.info(
        "[dashboard.scope] company_id=%s month_from=%s month_to=%s evaluation_month=%s elapsed_ms=%s",
        service_params.get("company_id") or "",
        service_params.get("month_from"),
        service_params.get("month_to"),
        service_params.get("evaluation_month"),
        int((time.perf_counter() - t0) * 1000),
    )

    shared_sales_df: pd.DataFrame | None = None
    source_monthly_actuals: list[dict[str, Any]] = []
    filter_diagnostics: list[dict[str, Any]] = []
    stock_timing = _stock_timing_meta({}, fallback_total_ms=0)
    if needs_sales_source or needs_stock_source:
        from app.services.analytics_sales_trend_service import get_sales_trend_df

        t_source = time.perf_counter()
        shared_sales_df = get_sales_trend_df(dict(source_params))
        source_elapsed_ms = int((time.perf_counter() - t_source) * 1000)
        t_filter = time.perf_counter()
        shared_sales_df = _filter_sales_source_for_dashboard(shared_sales_df, service_params)
        source_monthly_actuals = _monthly_sales_actuals_from_source(shared_sales_df)
        filter_elapsed_ms = int((time.perf_counter() - t_filter) * 1000)
        if isinstance(shared_sales_df, pd.DataFrame):
            filter_diagnostics.extend(list(shared_sales_df.attrs.get("dashboard_filter_diagnostics") or []))
        log.info(
            "[dashboard.source_load] source=shared_sales_source company_id=%s month_from=%s month_to=%s evaluation_month=%s rows=%s source_call_count=%s cache_used=%s manufacturer_test_filter_enabled=%s manufacturer_test_product_count=%s sales_source_sql_ms=%s product_master_merge_ms=%s product_filter_ms=%s elapsed_ms=%s",
            service_params.get("company_id") or "",
            service_params.get("month_from"),
            service_params.get("month_to"),
            service_params.get("evaluation_month"),
            0 if shared_sales_df is None else len(shared_sales_df),
            1,
            False,
            bool(manufacturer_test_codes),
            int(shared_sales_df["제품코드"].nunique()) if isinstance(shared_sales_df, pd.DataFrame) and "제품코드" in shared_sales_df.columns else 0,
            source_elapsed_ms,
            0,
            filter_elapsed_ms,
            int((time.perf_counter() - t_source) * 1000),
        )

    if manufacturer_summary_payload is None:
        from app.services.analytics_manufacturer_sales_trend_service import get_manufacturer_sales_trend_summary_result

        manufacturer_summary_payload = get_manufacturer_sales_trend_summary_result(
            dict(source_params),
            raw_df=shared_sales_df,
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

        visible_sales_df = _dashboard_visible_sales_df(shared_sales_df, service_params)
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
        stock_shortage_payload = get_stock_shortage_result(
            {
                **service_params,
                "month_to": service_params.get("evaluation_month"),
                "date_to": source_params.get("date_to"),
            },
            sales_raw_df=visible_sales_df,
            sales_forecast_df=shared_sales_forecast_df,
        )
        source_elapsed_ms = int((time.perf_counter() - t_source) * 1000)
        t_master_merge = time.perf_counter()
        stock_shortage_payload = _attach_dashboard_product_code_pairs(stock_shortage_payload, shared_sales_df)
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
        stock_shortage_payload = _attach_dashboard_product_code_pairs(stock_shortage_payload, shared_sales_df)
        stock_shortage_payload = _filter_payload_df_for_dashboard(stock_shortage_payload, service_params)
        source_df = _payload_df(stock_shortage_payload)
        filter_diagnostics.extend(list(source_df.attrs.get("dashboard_filter_diagnostics") or []))

    t_sales = time.perf_counter()
    sales = _build_sales_facts(
        manufacturer_summary_payload,
        history_actuals=source_monthly_actuals,
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
    t_stock = time.perf_counter()
    inventory = _build_inventory_facts(
        stock_shortage_payload,
        readiness_warning_pct=float(service_params.get("readiness_warning_pct") or STOCK_READY_THRESHOLD_PCT),
        evaluation_month=str(service_params.get("evaluation_month") or ""),
        policy_date=str(service_params.get("policy_date") or ""),
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
    turnover = {
        "status": "자료부족",
        "purchase_turnover_days": None,
        "sales_turnover_days": None,
        "definition": "최근 90일 정상 거래 고유 거래일 사이 평균 일수",
        "data_quality": ["입고/출고 정상 거래일 facts 미연결"],
    }
    t_actions = time.perf_counter()
    today_actions = _build_today_actions(sales, inventory, turnover)
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
        int((time.perf_counter() - t_actions) * 1000),
    )

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
        "stock_readiness": {
            "threshold_pct": float(service_params.get("readiness_warning_pct") or STOCK_READY_THRESHOLD_PCT),
            "policy": "제품별 준비가능수량=min(max(현재재고,0), 위험보정 잔여예상수요)",
            "adjustment_policy": "수요급증 품목은 현재 출고속도 기준 진행속도 보정 월말예상을 사용하고, 일반 품목은 기존 당월 예상수요를 사용",
            "sku_readiness_pct": inventory["metrics"]["sku_readiness_pct"],
        },
        "turnover_days": turnover,
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
    facts["source_call_count"] = int(needs_sales_source) + int(needs_stock_source)
    log.info(
        "[dashboard.finish] company_id=%s month_from=%s month_to=%s evaluation_month=%s source_call_count=%s dashboard_total_ms=%s elapsed_ms=%s",
        service_params.get("company_id") or "",
        service_params.get("month_from"),
        service_params.get("month_to"),
        service_params.get("evaluation_month"),
        facts["source_call_count"],
        int((time.perf_counter() - t0) * 1000),
        int((time.perf_counter() - t0) * 1000),
    )
    return facts

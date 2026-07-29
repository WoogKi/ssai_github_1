# app/sims/views/dashboard_lite.py
# -*- coding: utf-8 -*-
"""Dashboard Lite v0.1 Streamlit view."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import html
import json
import logging
import re
import time
import uuid
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from app.services.dashboard_lite_facts import (
    build_dashboard_lite_facts,
    default_dashboard_lite_scope,
    filter_dashboard_risk_detail_rows,
    normalize_dashboard_lite_params,
)
from app.services.dashboard_risk_detail_export import build_dashboard_risk_detail_excel_bytes
from app.services.product_supplier_scope_service import (
    SCOPE_MANUFACTURER,
    SCOPE_ORDER_VENDOR,
    apply_product_supplier_scope,
    load_supplier_manager_options,
    normalize_product_supplier_scope,
    resolve_supplier_vendor_codes,
    supplier_scope_fingerprint,
)
from app.services.ssai_analysis_profile_service import (
    COMPANY_DEFAULT_KEYS,
    PROFILE_PERMISSION,
    build_company_default_adapter,
    mark_analysis_profile_saved,
    load_dashboard_profile,
    save_dashboard_profile,
)
from app.services import rddbc010_service as C01
from app.db.mssql_client import query_to_df
from app.sims.views.rddbc_io_shared import _load_stock_code_options
from app.ui.chat_middleware import get_current_chat_room_id
from app.ui.ssai_login import require_permission


log = logging.getLogger("ssai.sims.dashboard_lite")

DASHBOARD_LITE_SESSION_KEYS = (
    "__dashboard_lite_result",
    "__dashboard_lite_run_seq",
    "__dashboard_lite_profile_loaded_for",
    "__dashboard_lite_styles_loaded",
    "__dashboard_lite_stock_labels",
    "__dashboard_lite_stock_mode",
    "__dashboard_lite_vendor_group_list",
    "__dashboard_lite_vendor_kind_list",
    "__dashboard_lite_product_group_list",
    "__dashboard_lite_product_di_list",
    "__dashboard_lite_product_class_list",
    "__dashboard_lite_io_gu_list",
    "__dashboard_lite_major_purchase_vendor_days",
    "__dashboard_lite_risk_analysis_days",
    "__dashboard_lite_overstock_inactive_days",
    "__dashboard_lite_readiness_warning_pct",
    "__dashboard_lite_risk_quick_view_count",
    "__dashboard_lite_amount_display_unit",
    "__dashboard_lite_manufacturer_text",
    "__dashboard_lite_manufacturer_test_codes",
    "__dashboard_lite_manufacturer_resolved_code",
    "__dashboard_lite_manufacturer_resolved_name",
    "__dashboard_lite_manufacturer_candidates",
    "__dashboard_lite_manufacturer_candidate_code",
    "__dashboard_lite_product_supplier_scope_mode",
    "__dashboard_lite_order_vendor_text",
    "__dashboard_lite_manufacturer_manager_codes",
    "__dashboard_lite_purchase_manager_codes",
    "__dashboard_lite_supplier_scope",
    "__dashboard_lite_exclude_product_group_list",
    "__dashboard_lite_exclude_product_di_list",
    "__dashboard_lite_exclude_product_class_list",
    "__dashboard_lite_risk_detail_excel_cache",
    "__dashboard_selected_action_detail",
    "__dashboard_lite_suppress_chat_autoscroll_once",
)

DASHBOARD_LITE_OPTION_CACHE_KEY = "__dashboard_lite_scope_options"
DASHBOARD_LITE_OPTION_CACHE_VERSION = 3
_DASHBOARD_RENDER_TARGET: Any | None = None

_DASHBOARD_PROFILE_WIDGETS = {
    "stock_mode": "__dashboard_lite_stock_mode",
    "stock_cd_list": "__dashboard_lite_stock_labels",
    "vendor_group_list": "__dashboard_lite_vendor_group_list",
    "vendor_kind_list": "__dashboard_lite_vendor_kind_list",
    "product_group_list": "__dashboard_lite_product_group_list",
    "product_di_list": "__dashboard_lite_product_di_list",
    "product_class_list": "__dashboard_lite_product_class_list",
    "io_gu_list": "__dashboard_lite_io_gu_list",
    "major_purchase_vendor_days": "__dashboard_lite_major_purchase_vendor_days",
    "risk_analysis_days": "__dashboard_lite_risk_analysis_days",
    "overstock_inactive_days": "__dashboard_lite_overstock_inactive_days",
    "readiness_warning_pct": "__dashboard_lite_readiness_warning_pct",
    "risk_quick_view_count": "__dashboard_lite_risk_quick_view_count",
    "amount_display_unit": "__dashboard_lite_amount_display_unit",
}

_DASHBOARD_PROFILE_SCALAR_DEFAULTS = {
    "stock_mode": "real",
    "major_purchase_vendor_days": 90,
    "risk_analysis_days": 90,
    "overstock_inactive_days": 90,
    "readiness_warning_pct": 98.0,
    "risk_quick_view_count": 30,
    "amount_display_unit": "auto",
}


def set_dashboard_lite_render_target(target: Any | None) -> None:
    """Set the main-page container used for the one Dashboard result render."""
    global _DASHBOARD_RENDER_TARGET
    _DASHBOARD_RENDER_TARGET = target


def clear_dashboard_lite_session_state(session_state: Any, namespace: str | None = None) -> list[str]:
    """Clear Dashboard Lite widgets, facts, and cache without touching login/company state."""
    removed: list[str] = []
    for key in DASHBOARD_LITE_SESSION_KEYS:
        if key in session_state:
            session_state.pop(key, None)
            removed.append(key)
    keys = list(session_state.keys()) if hasattr(session_state, "keys") else []
    for key in keys:
        text = str(key)
        if not text.startswith("__dashboard_lite_"):
            continue
        session_state.pop(key, None)
        removed.append(text)
    return removed


def clear_dashboard_lite_active_result(session_state: Any) -> list[str]:
    """Drop only the room-bound interactive Dashboard result on room changes."""
    removed: list[str] = []
    for key in list(session_state.keys()):
        text = str(key)
        if text in {"__dashboard_lite_result", "__dashboard_selected_action_detail", "__dashboard_lite_suppress_chat_autoscroll_once"} or text.startswith("__dashboard_lite_risk_detail_"):
            session_state.pop(key, None)
            removed.append(text)
    return removed


def dashboard_lite_primary_cache_matches_context(
    cache: Any,
    *,
    current_room_id: Any,
    current_company_id: Any,
) -> dict[str, bool]:
    """Fail closed unless an interactive Dashboard belongs to this room/company."""
    source = cache if isinstance(cache, dict) else {}
    cache_room_id = str(source.get("room_id") or "").strip()
    cache_company_id = str(source.get("company_id") or "").strip()
    room_id = str(current_room_id or "").strip()
    company_id = str(current_company_id or "").strip()
    return {
        "cache_available": bool(source),
        "room_match": bool(cache_room_id and room_id and cache_room_id == room_id),
        "company_match": bool(cache_company_id and company_id and cache_company_id == company_id),
    }


def dashboard_lite_chat_render_decision(chat_cache: Any, chat_meta: Any) -> dict[str, Any]:
    """Choose primary or compact rendering at the message's chronological position."""
    cache = chat_cache if isinstance(chat_cache, dict) else {}
    meta = chat_meta if isinstance(chat_meta, dict) else {}
    primary = st.session_state.get("__dashboard_lite_result")
    primary_available = isinstance(primary, dict) and isinstance(primary.get("facts"), dict)
    primary = primary if primary_available else {}

    chat_room_id = str(meta.get("room_id") or cache.get("room_id") or "").strip()
    primary_room_id = str(primary.get("room_id") or "").strip()
    room_match = bool(chat_room_id and primary_room_id and chat_room_id == primary_room_id)

    chat_event_id = str(meta.get("dashboard_event_id") or cache.get("dashboard_event_id") or "").strip()
    primary_event_id = str(primary.get("dashboard_event_id") or "").strip()
    event_match = bool(chat_event_id and primary_event_id and chat_event_id == primary_event_id)

    chat_fingerprint = str(cache.get("query_fingerprint") or cache.get("cache_key") or "").strip()
    primary_fingerprint = str(primary.get("query_fingerprint") or primary.get("cache_key") or "").strip()
    fingerprint_match = bool(chat_fingerprint and primary_fingerprint and chat_fingerprint == primary_fingerprint)
    company_match = bool(
        str(cache.get("company_id") or "").strip()
        and str(primary.get("company_id") or "").strip()
        and str(cache.get("company_id") or "").strip() == str(primary.get("company_id") or "").strip()
    )
    fallback_match = (
        (not chat_event_id or not primary_event_id)
        and company_match
        and fingerprint_match
    )
    render_primary = bool(
        primary_available
        and room_match
        and company_match
        and (event_match or fallback_match)
    )
    action = "render_active_primary" if render_primary else "render_snapshot"
    render_mode = "primary" if render_primary else "chat"
    log.info(
        "[dashboard.chat_render] action=%s primary_available=%s room_match=%s event_match=%s "
        "fingerprint_match=%s render_mode=%s skipped=False",
        action,
        primary_available,
        room_match,
        event_match,
        fingerprint_match,
        render_mode,
    )
    return {
        "action": action,
        "primary_available": primary_available,
        "room_match": room_match,
        "company_match": company_match,
        "event_match": event_match,
        "fingerprint_match": fingerprint_match,
        "render_mode": render_mode,
        "render_cache": primary if render_primary else cache,
        "skipped": False,
    }


def _clean_list(values: Any) -> list[str]:
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


def _prepare_dashboard_multiselect_state(widget_key: str, options: Any) -> list[str]:
    """Keep restored selections, dropping only values absent from current options."""
    valid_options = set(_clean_list(options))
    current_values = _clean_list(st.session_state.get(widget_key))
    st.session_state[widget_key] = [
        value for value in current_values if value in valid_options
    ]
    return list(st.session_state[widget_key])


def _prepare_dashboard_profile_scalar_state() -> None:
    """Initialize profile-managed scalar widgets before Streamlit creates them."""
    for source_key, default_value in _DASHBOARD_PROFILE_SCALAR_DEFAULTS.items():
        widget_key = _DASHBOARD_PROFILE_WIDGETS[source_key]
        if widget_key not in st.session_state:
            st.session_state[widget_key] = default_value


def _normalized_key_list(values: Any) -> list[str]:
    return sorted(_clean_list(values))


def _summarize_labels(labels: list[str], empty_text: str) -> str:
    labels = _clean_list(labels)
    if not labels:
        return empty_text
    first = labels[0]
    if len(labels) == 1:
        return first
    return f"{first} 외 {len(labels) - 1}개"


def _option_label(code: Any, name: Any) -> str:
    code_text = str(code or "").strip()
    name_text = str(name or "").strip()
    display_code = code_text.split(":", 1)[1] if ":" in code_text else code_text
    if code_text and name_text:
        return f"{display_code} - {name_text}"
    return code_text or name_text


def _fmt_number(value: Any, digits: int = 0) -> str:
    if value is None:
        return "자료부족"
    try:
        if pd.isna(value):
            return "자료부족"
        num = float(value)
    except Exception:
        return "자료부족"
    if digits <= 0:
        return f"{num:,.0f}"
    return f"{num:,.{digits}f}"


def _fmt_threshold_pct(value: Any) -> str:
    """Format an operational threshold without inventing trailing precision."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "자료부족"
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _amount_display_spec(unit: str, value: Any) -> tuple[float, str]:
    """Choose a presentation-only amount scale without changing facts values."""
    normalized = str(unit or "auto").strip().lower()
    if normalized == "thousand":
        return 1_000.0, "천원"
    if normalized == "million":
        return 1_000_000.0, "백만원"
    if normalized == "auto":
        try:
            magnitude = abs(float(value))
        except Exception:
            magnitude = 0.0
        if magnitude >= 1_000_000:
            return 1_000_000.0, "백만원"
        if magnitude >= 1_000:
            return 1_000.0, "천원"
    return 1.0, "원"


def _resolved_dashboard_amount_unit(facts: dict[str, Any], requested_unit: Any) -> str:
    """Resolve auto once per event so a later widget change cannot restyle it."""
    requested = str(requested_unit or "auto").strip().lower()
    if requested in {"won", "thousand", "million"}:
        return requested

    values: list[float] = []

    def _collect(value: Any) -> None:
        if isinstance(value, dict):
            unit = str(value.get("unit") or "")
            if unit in {"원", "금액"}:
                try:
                    values.append(abs(float(value.get("value") or 0)))
                except (TypeError, ValueError):
                    pass
            for key, child in value.items():
                normalized_key = str(key).lower()
                if "금액" in str(key) or "amount" in normalized_key:
                    try:
                        values.append(abs(float(child or 0)))
                        continue
                    except (TypeError, ValueError):
                        pass
                if isinstance(child, (dict, list, tuple)):
                    _collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                _collect(child)

    _collect(facts)
    divisor, _label = _amount_display_spec("auto", max(values, default=0.0))
    return "million" if divisor == 1_000_000 else ("thousand" if divisor == 1_000 else "won")


def _facts_amount_display_unit(facts: dict[str, Any]) -> str:
    filters = facts.get("filters") or {}
    return str(
        filters.get("amount_display_unit_resolved")
        or filters.get("amount_display_unit")
        or "auto"
    )


def _fmt_dashboard_amount(value: Any, unit: str) -> str:
    if value is None:
        return _fmt_number(value)
    try:
        divisor, label = _amount_display_spec(unit, value)
        digits = 1 if divisor > 1 else 0
        return f"{_fmt_number(float(value) / divisor, digits)} {label}"
    except Exception:
        return _fmt_number(value)


def _metric_card(label: str, value: Any, suffix: str = "", *, help_text: str = "", digits: int = 0, amount_unit: str = "") -> None:
    display_value = _fmt_dashboard_amount(value, amount_unit) if amount_unit else _fmt_number(value, digits)
    if display_value != "자료부족":
        display_value = f"{display_value}{suffix}"
    safe_label = html.escape(str(label or ""))
    safe_value = html.escape(str(display_value or ""))
    safe_help = html.escape(str(help_text or ""))
    help_html = f'<div class="dashboard-lite-kpi-help">{safe_help}</div>' if safe_help else ""
    st.markdown(
        f"""
        <div class="dashboard-lite-kpi-card">
          <div class="dashboard-lite-kpi-label">{safe_label}</div>
          <div class="dashboard-lite-kpi-value">{safe_value}</div>
          {help_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _inject_dashboard_lite_styles_once() -> None:
    # Streamlit rerun rebuilds the DOM, so keep this Dashboard-scoped style in
    # the render path instead of relying on a once-per-session injection.
    st.markdown(
        """
        <style>
        .dashboard-lite-kpi-card {
            border: 1px solid rgba(49, 51, 63, 0.18);
            border-radius: 8px;
            padding: 12px 14px;
            min-height: 104px;
            background: #ffffff;
        }
        .dashboard-lite-kpi-label {
            color: rgba(49, 51, 63, 0.74);
            font-size: 0.9rem;
            line-height: 1.25;
            text-align: left;
            min-height: 2.4em;
        }
        .dashboard-lite-kpi-value {
            margin-top: 10px;
            text-align: right;
            font-size: 1.45rem;
            font-weight: 700;
            line-height: 1.2;
            font-variant-numeric: tabular-nums;
        }
        .dashboard-lite-kpi-help {
            margin-top: 8px;
            color: rgba(49, 51, 63, 0.55);
            font-size: 0.75rem;
            text-align: left;
        }
        .dashboard-lite-sales-state {
            margin: 10px 0 14px;
            padding: 9px 12px;
            border-left: 3px solid #0f766e;
            background: #f7fbfb;
            color: rgba(49, 51, 63, 0.82);
            font-size: 0.86rem;
            line-height: 1.55;
        }
        .dashboard-lite-sales-brief {
            margin: 14px 0 18px;
            padding: 16px 18px;
            border: 1px solid rgba(15, 118, 110, 0.22);
            border-radius: 8px;
            background: #fbfdfd;
        }
        .dashboard-lite-sales-brief-title {
            margin: 0 0 8px;
            color: #1f2937;
            font-size: 1.125rem;
            font-weight: 700;
            line-height: 1.35;
        }
        .dashboard-lite-sales-brief-line {
            margin: 4px 0;
            color: rgba(31, 41, 55, 0.9);
            font-size: 1rem;
            line-height: 1.6;
        }
        .dashboard-lite-sales-brief-note {
            margin: 8px 0 0;
            color: rgba(49, 51, 63, 0.62);
            font-size: 0.82rem;
            line-height: 1.45;
        }
        .dashboard-lite-sales-gauge {
            min-height: 244px;
            padding: 6px 10px 10px;
            border: 1px solid rgba(49, 51, 63, 0.16);
            border-radius: 8px;
            text-align: center;
            background: #ffffff;
        }
        .dashboard-lite-sales-gauge svg {
            display: block;
            width: min(100%, 260px);
            margin: 0 auto -24px;
        }
        .dashboard-lite-gauge-main {
            color: #1f2937;
            font-size: 1.6rem;
            font-weight: 700;
            font-variant-numeric: tabular-nums;
        }
        .dashboard-lite-gauge-label, .dashboard-lite-gauge-sub {
            color: rgba(49, 51, 63, 0.68);
            font-size: 0.8rem;
            line-height: 1.45;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.session_state["__dashboard_lite_styles_loaded"] = True


def _sales_time_status_label(value: Any) -> str:
    try:
        achievement = float(value)
    except (TypeError, ValueError):
        return "계산불가"
    if achievement >= 105.0:
        return "현재일 예상보다 앞섬"
    if achievement >= 95.0:
        return "현재일 예상과 유사"
    return "현재일 예상보다 뒤처짐"


def _sales_presentation_state(facts: dict[str, Any]) -> dict[str, Any]:
    """Derive display-only sales labels without changing stored Dashboard facts."""
    sales = facts.get("sales") or {}
    metrics = sales.get("metrics") or {}
    visualization = sales.get("visualization") or {}
    current = (metrics.get("current_month_sales") or {}).get("value")
    forecast = (metrics.get("current_month_forecast_sales") or {}).get("value")
    time_progress = (metrics.get("time_progress_pct") or {}).get("value")
    achievement = (metrics.get("time_adjusted_achievement_pct") or {}).get("value")
    try:
        current_number = float(current)
        forecast_number = float(forecast)
    except (TypeError, ValueError):
        current_number = forecast_number = None

    if current_number is None or forecast_number is None:
        comparison_label, comparison_amount = "월말 예상 비교 자료부족", None
    elif current_number > forecast_number:
        comparison_label, comparison_amount = "월말 예상 초과", current_number - forecast_number
    elif current_number < forecast_number:
        comparison_label, comparison_amount = "월말 예상 잔여", forecast_number - current_number
    else:
        comparison_label, comparison_amount = "월말 예상 도달", 0.0

    elapsed_days = total_days = None
    evaluation_month = str(visualization.get("evaluation_month") or "")
    time_basis = str((metrics.get("time_progress_pct") or {}).get("time_basis") or "")
    day_match = re.search(r"(\d+)\s*/\s*(\d+)", time_basis)
    if day_match:
        elapsed_days, total_days = int(day_match.group(1)), int(day_match.group(2))
    elif evaluation_month and time_progress is not None:
        try:
            total_days = monthrange(int(evaluation_month[:4]), int(evaluation_month[4:6]))[1]
            elapsed_days = round(total_days * float(time_progress) / 100.0)
        except (ValueError, TypeError):
            elapsed_days = total_days = None

    return {
        "current_sales": current,
        "forecast_sales": forecast,
        "expected_to_date_sales": visualization.get("expected_to_date_sales"),
        "sales_progress_pct": visualization.get("sales_progress_pct", (metrics.get("current_month_progress_pct") or {}).get("value")),
        "time_progress_pct": time_progress,
        "time_adjusted_achievement_pct": achievement,
        "time_adjusted_status": _sales_time_status_label(achievement),
        "comparison_label": comparison_label,
        "comparison_amount": comparison_amount,
        "elapsed_days": elapsed_days,
        "total_days": total_days,
    }


def _sales_gauge_state(facts: dict[str, Any]) -> dict[str, Any]:
    """Return display-only gauge bounds; current-day progress stays calendar based."""
    state = _sales_presentation_state(facts)
    try:
        progress = max(0.0, float(state.get("sales_progress_pct") or 0.0))
    except (TypeError, ValueError):
        progress = 0.0
    try:
        today_progress = max(0.0, float(state.get("time_adjusted_achievement_pct") or 0.0))
    except (TypeError, ValueError):
        today_progress = 0.0
    maximum = max(120.0, min(max(progress, today_progress, 100.0) + 10.0, 250.0))
    return {**state, "gauge_max_pct": maximum, "needle_pct": progress, "today_marker_pct": today_progress, "time_basis_label": "현재일 기준"}


def _render_sales_gauge(facts: dict[str, Any]) -> None:
    _inject_dashboard_lite_styles_once()
    state = _sales_gauge_state(facts)
    amount_unit = _facts_amount_display_unit(facts)
    progress = float(state["needle_pct"])
    maximum = float(state["gauge_max_pct"])
    needle_angle = max(-90.0, min(90.0, -90.0 + min(progress, maximum) / maximum * 180.0))
    today_angle = max(-90.0, min(90.0, -90.0 + min(float(state["today_marker_pct"]), maximum) / maximum * 180.0))
    comparison = state["comparison_label"]
    comparison_amount = state.get("comparison_amount")
    comparison_text = comparison if comparison_amount is None or comparison == "월말 예상 도달" else f"{comparison} {_fmt_dashboard_amount(comparison_amount, amount_unit)}"
    st.markdown("### 평가월 매출 진행")
    st.markdown(
        f"""
        <div class="dashboard-lite-sales-gauge">
          <svg viewBox="0 0 240 138" role="img" aria-label="평가월 매출 진행">
            <path d="M 24 116 A 96 96 0 0 1 216 116" fill="none" stroke="#e5e7eb" stroke-width="18" stroke-linecap="round"/>
            <path d="M 120 20 L 120 39" stroke="#f59e0b" stroke-width="4" transform="rotate(0 120 116)"/>
            <path d="M 120 23 L 120 43" stroke="#0f766e" stroke-width="4" transform="rotate({today_angle:.3f} 120 116)"/>
            <path d="M 120 116 L 120 42" stroke="#2563eb" stroke-width="5" stroke-linecap="round" transform="rotate({needle_angle:.3f} 120 116)"/>
            <circle cx="120" cy="116" r="7" fill="#2563eb"/>
          </svg>
          <div class="dashboard-lite-gauge-main">{html.escape(_fmt_number(progress, 1))}%</div>
          <div class="dashboard-lite-gauge-label">월말 예상 달성률</div>
          <div class="dashboard-lite-gauge-sub">{html.escape(state['time_basis_label'])} 달성률 {html.escape(_fmt_number(state['today_marker_pct'], 1))}%</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    for label, value in (("현재매출", state["current_sales"]), ("월말 예상매출", state["forecast_sales"]), ("오늘 기준 예상매출", state["expected_to_date_sales"])):
        st.caption(f"{label}: {_fmt_dashboard_amount(value, amount_unit)}")
    st.caption(comparison_text)


def _render_status_cards(facts: dict[str, Any]) -> None:
    _inject_dashboard_lite_styles_once()
    state = _sales_presentation_state(facts)
    amount_unit = _facts_amount_display_unit(facts)
    cards = [
        ("당월 현재매출", state["current_sales"], "당월 누적 실적", "amount"),
        ("월말 예상매출", state["forecast_sales"], "당월 월말 예상", "amount"),
        ("현재일 기준 예상매출", state["expected_to_date_sales"], "월 경과율 반영", "amount"),
        ("현재일 기준 달성률", state["time_adjusted_achievement_pct"], state["time_adjusted_status"], "pct"),
    ]
    if amount_unit == "auto":
        amount_values = [value for _label, value, _help, kind in cards if kind == "amount"]
        try:
            max_amount = max(abs(float(value)) for value in amount_values if value is not None)
        except ValueError:
            max_amount = 0.0
        divisor, _label = _amount_display_spec("auto", max_amount)
        amount_unit = "million" if divisor == 1_000_000 else ("thousand" if divisor == 1_000 else "won")

    cols = st.columns(4)
    for col, (label, value, help_text, kind) in zip(cols, cards):
        with col:
            _metric_card(
                label,
                value,
                "%" if kind == "pct" else "",
                help_text=help_text,
                digits=1 if kind == "pct" else 0,
                amount_unit=amount_unit if kind == "amount" else "",
            )

    progress_text = "자료부족"
    if state["elapsed_days"] is not None and state["total_days"] is not None and state["time_progress_pct"] is not None:
        progress_text = f"{state['elapsed_days']}일 / {state['total_days']}일 · {_fmt_number(state['time_progress_pct'], 1)}%"
    sales_progress_text = _fmt_number(state["sales_progress_pct"], 1)
    comparison_text = state["comparison_label"]
    if state["comparison_amount"] is not None and state["comparison_label"] != "월말 예상 도달":
        comparison_text = f"{comparison_text} {_fmt_dashboard_amount(state['comparison_amount'], amount_unit)}"
    st.markdown(
        "<div class=\"dashboard-lite-sales-state\">"
        f"<strong>월 경과</strong>: {html.escape(progress_text)} · "
        f"<strong>월말 예상 달성률</strong>: {html.escape(sales_progress_text)}% · "
        f"<strong>{html.escape(comparison_text)}</strong>"
        "</div>",
        unsafe_allow_html=True,
    )


def _render_sales_brief(facts: dict[str, Any]) -> None:
    sales = facts.get("sales") or {}
    metrics = sales.get("metrics") or {}
    visualization = sales.get("visualization") or {}
    current = metrics.get("current_month_sales") or {}
    forecast = metrics.get("current_month_forecast_sales") or {}
    state = _sales_presentation_state(facts)
    amount_unit = _facts_amount_display_unit(facts)
    current_value = current.get("value")
    forecast_value = forecast.get("value")
    if current_value is None or forecast_value is None:
        st.caption("당월 현재·예상 매출 자료가 부족합니다.")
        return

    if state["comparison_label"] == "월말 예상 초과":
        lines = [
            f"현재 매출은 <strong>{html.escape(_fmt_dashboard_amount(current_value, amount_unit))}</strong>이며, 월말 예상 <strong>{html.escape(_fmt_dashboard_amount(forecast_value, amount_unit))}</strong>을 <strong>{html.escape(_fmt_dashboard_amount(state['comparison_amount'], amount_unit))}</strong> 초과했습니다.",
        ]
    elif state["comparison_label"] == "월말 예상 도달":
        lines = [
            f"현재 매출은 <strong>{html.escape(_fmt_dashboard_amount(current_value, amount_unit))}</strong>이며, 월말 예상 <strong>{html.escape(_fmt_dashboard_amount(forecast_value, amount_unit))}</strong>에 도달했습니다.",
        ]
    else:
        lines = [
            f"현재 매출은 <strong>{html.escape(_fmt_dashboard_amount(current_value, amount_unit))}</strong>이며, 월말 예상은 <strong>{html.escape(_fmt_dashboard_amount(forecast_value, amount_unit))}</strong>입니다. 월말 예상까지 <strong>{html.escape(_fmt_dashboard_amount(state['comparison_amount'], amount_unit))}</strong>이 남았습니다.",
        ]

    expected_to_date = state["expected_to_date_sales"]
    if expected_to_date is None:
        lines.append("현재일 기준 예상매출을 계산할 수 있는 평가월 자료가 부족합니다.")
    else:
        difference = float(current_value) - float(expected_to_date)
        if state["time_adjusted_status"] == "현재일 예상과 유사":
            lines.append("현재일 기준 예상매출과 유사한 수준입니다.")
        elif difference > 0:
            lines.append(f"현재일 기준 예상매출보다 <strong>{html.escape(_fmt_dashboard_amount(abs(difference), amount_unit))}</strong> 앞서 있습니다.")
        else:
            lines.append(f"현재일 기준 예상매출보다 <strong>{html.escape(_fmt_dashboard_amount(abs(difference), amount_unit))}</strong> 뒤처져 있습니다.")

    chart_rows = sales.get("chart_rows") or []
    completed = [row for row in chart_rows if row.get("kind") == "완료월 실제"]
    preforecast = [row for row in chart_rows if row.get("kind") == "완료월 사전예상"]
    preforecast_by_month = {str(row.get("period_sort") or ""): row for row in preforecast}
    comparable = [row for row in completed if str(row.get("period_sort") or "") in preforecast_by_month]
    if comparable:
        latest = max(comparable, key=lambda row: str(row.get("period_sort") or ""))
        prior = preforecast_by_month[str(latest.get("period_sort") or "")]
        prior_value = float(prior.get("value") or 0)
        if prior_value:
            delta_pct = (float(latest.get("value") or 0) / prior_value - 1.0) * 100.0
            direction = "높았습니다" if delta_pct > 0 else ("낮았습니다" if delta_pct < 0 else "같았습니다")
            lines.append(f"최근 완료월 실적은 당시 사전예상보다 <strong>{html.escape(_fmt_number(abs(delta_pct), 1))}%</strong> {direction}.")
        else:
            lines.append("최근 완료월의 비교 가능한 사전예상 자료가 없습니다.")
    else:
        lines.append("최근 완료월의 비교 가능한 사전예상 자료가 없습니다.")
    st.markdown(
        "<div class=\"dashboard-lite-sales-brief\">"
        "<div class=\"dashboard-lite-sales-brief-title\">오늘의 매출 요약</div>"
        + "".join(f"<div class=\"dashboard-lite-sales-brief-line\">{line}</div>" for line in lines)
        + "<div class=\"dashboard-lite-sales-brief-note\">현재일 기준 예상매출은 평가월의 월 경과율을 반영한 참고값입니다.</div>"
        + "</div>",
        unsafe_allow_html=True,
    )


def _build_sales_bar_chart(facts: dict[str, Any]) -> alt.Chart | alt.LayerChart | None:
    """Build actual bars with monthly forecast and current-day target markers."""
    rows = (facts.get("sales") or {}).get("chart_rows") or []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if df.empty or not {"period", "period_sort", "kind", "value"}.issubset(df.columns):
        return None
    df = df.sort_values(["period_sort", "kind"], kind="stable").reset_index(drop=True)
    period_lookup = (
        df[["period", "period_sort"]]
        .drop_duplicates()
        .sort_values("period_sort", kind="stable")
    )
    first_year = str(period_lookup.iloc[0]["period_sort"])[:4]
    period_lookup["display_period"] = period_lookup.apply(
        lambda row: (
            f"{str(row['period_sort'])[:4]}년 {int(str(row['period_sort'])[4:6])}월"
            if str(row["period_sort"])[:4] != first_year
            else f"{int(str(row['period_sort'])[4:6])}월"
        ),
        axis=1,
    )
    period_display_map = dict(zip(period_lookup["period"], period_lookup["display_period"]))
    period_order = period_lookup["display_period"].tolist()
    df["display_period"] = df["period"].map(period_display_map).fillna(df["period"])

    amount_unit = _facts_amount_display_unit(facts)
    divisor, unit_label = _amount_display_spec(amount_unit, pd.to_numeric(df["value"], errors="coerce").abs().max())
    df["display_value"] = pd.to_numeric(df["value"], errors="coerce") / divisor
    actual_df = df[~df["kind"].astype(str).str.contains("예상", na=False)].copy()
    forecast_df = df[df["kind"].astype(str).str.contains("예상", na=False)].copy()
    actual_df["series"] = "실제매출"
    actual_df["value_kind"] = actual_df["kind"].map(
        {"완료월 실제": "완료월 실제", "당월 현재(부분월)": "당월 현재매출"}
    ).fillna("실제매출")
    forecast_df["series"] = "예상매출"
    forecast_df["value_kind"] = forecast_df["kind"].map(
        {"완료월 사전예상": "완료월 사전예상", "당월 예상": "당월 월말 예상"}
    ).fillna("예상매출")
    forecast_df = forecast_df[pd.to_numeric(forecast_df["value"], errors="coerce").notna()].copy()
    tooltip = [
        alt.Tooltip("display_period:N", title="기준월"),
        alt.Tooltip("value_kind:N", title="값 종류"),
        alt.Tooltip("display_value:Q", title=f"매출 ({unit_label})", format=",.0f"),
        alt.Tooltip("value:Q", title="원본 금액(원)", format=",.0f"),
        alt.Tooltip("amount_display_unit:N", title="표시 단위"),
        alt.Tooltip("month_status:N", title="월 구분"),
        alt.Tooltip("partial_period:N", title="부분월"),
        alt.Tooltip("forecast_status:N", title="예상 상태"),
        alt.Tooltip("forecast_basis:N", title="예상 기준"),
    ]
    df["amount_display_unit"] = unit_label
    actual_df["amount_display_unit"] = unit_label
    forecast_df["amount_display_unit"] = unit_label
    actual_df["month_status"] = actual_df["kind"].map(
        {"완료월 실제": "완료월", "당월 현재(부분월)": "평가월"}
    ).fillna("실제")
    forecast_df["month_status"] = forecast_df["kind"].map(
        {"완료월 사전예상": "완료월", "당월 예상": "평가월"}
    ).fillna("예상")
    series_color = alt.Color(
        "series:N",
        title=None,
        scale=alt.Scale(
            domain=["실제매출", "예상매출", "현재일 기준"],
            range=["#2563eb", "#f97316", "#0f766e"],
        ),
        legend=alt.Legend(orient="top", direction="horizontal", labelFontSize=12, symbolSize=90),
    )
    x_encoding = alt.X("display_period:N", title=None, sort=period_order, axis=alt.Axis(labelAngle=0, labelPadding=8))
    y_encoding = alt.Y("display_value:Q", title=f"매출 ({unit_label})", stack=None, axis=alt.Axis(grid=True, gridColor="#e5e7eb", gridOpacity=0.8))
    base = alt.Chart(df).encode(
        x=x_encoding,
        y=y_encoding,
        tooltip=tooltip,
    )
    layers = []
    if not actual_df.empty:
        layers.append(
            alt.Chart(actual_df).mark_bar(opacity=0.9, size=32).encode(
                x=x_encoding,
                y=y_encoding,
                color=series_color,
                tooltip=tooltip,
            )
        )
    if not forecast_df.empty:
        layers.append(
            alt.Chart(forecast_df).mark_tick(orient="horizontal", thickness=3, size=34).encode(
                x=x_encoding,
                y=y_encoding,
                color=series_color,
                tooltip=tooltip,
            )
        )
    chart = (
        alt.layer(*layers).resolve_scale(y="shared", color="shared").properties(height=380, padding={"left": 4, "right": 8, "top": 8, "bottom": 0})
        if layers else base.mark_bar().properties(height=380)
    )
    visualization = (facts.get("sales") or {}).get("visualization") or {}
    marker_period = str(visualization.get("evaluation_month") or "")
    marker_value = visualization.get("expected_to_date_sales")
    if marker_period and marker_value is not None and visualization.get("time_progress_pct") not in (None, 0, 100):
        marker_period_label = period_display_map.get(f"{marker_period[:4]}-{marker_period[4:6]}") or f"{int(marker_period[4:6])}월"
        marker_df = pd.DataFrame(
            [{
                "display_period": marker_period_label,
                "display_value": float(marker_value) / divisor,
                "value": float(marker_value),
                "series": "현재일 기준",
                "value_kind": "현재일 기준 예상매출",
                "amount_display_unit": unit_label,
                "month_status": "평가월",
                "time_progress_pct": visualization.get("time_progress_pct"),
            }]
        )
        marker = alt.Chart(marker_df).mark_tick(orient="horizontal", thickness=3, size=34).encode(
            x=x_encoding,
            y=y_encoding,
            color=series_color,
            tooltip=tooltip + [alt.Tooltip("time_progress_pct:Q", title="월 경과율", format=".1f")],
        )
        chart = chart + marker
    return chart


def _render_sales_chart(facts: dict[str, Any]) -> None:
    chart = _build_sales_bar_chart(facts)
    if chart is None:
        st.info("매출 그래프를 표시할 완료월/당월 facts가 없습니다.")
        return
    st.altair_chart(chart, width="stretch")


def _build_stock_readiness_chart(facts: dict[str, Any]) -> alt.Chart | alt.LayerChart | None:
    inventory = facts.get("inventory") or {}
    rows = inventory.get("risk_targets") or []
    threshold_value = float((facts.get("stock_readiness") or {}).get("threshold_pct") or 98.0)
    if not rows:
        return None
    df = pd.DataFrame(rows).head(10).copy()
    if df.empty:
        return None
    for field, fallback in (("product_code", ""), ("product_name", "제품명 미확인"), ("재고위험상태", "판정 제외"), ("current_stock_qty", None), ("주요매입처명", ""), ("수요급증여부", False), ("위험보정기준", "")):
        if field not in df.columns:
            df[field] = fallback
    df["display_readiness_pct"] = pd.to_numeric(
        df.get("위험보정재고준비율", df.get("stock_readiness_pct")), errors="coerce"
    ).fillna(pd.to_numeric(df.get("stock_readiness_pct"), errors="coerce"))
    df["display_remaining_demand_qty"] = pd.to_numeric(
        df.get("위험보정잔여예상수요", df.get("remaining_expected_demand_qty")), errors="coerce"
    ).fillna(pd.to_numeric(df.get("remaining_expected_demand_qty"), errors="coerce"))
    df["display_shortage_qty"] = pd.to_numeric(
        df.get("위험보정부족예상수량", df.get("shortage_qty")), errors="coerce"
    ).fillna(pd.to_numeric(df.get("shortage_qty"), errors="coerce"))
    df["display_shortage_amt"] = pd.to_numeric(
        df.get("위험보정부족예상금액", df.get("shortage_amt")), errors="coerce"
    ).fillna(pd.to_numeric(df.get("shortage_amt"), errors="coerce"))
    df["display_readiness_label"] = df["display_readiness_pct"].map(lambda value: f"{float(value):.1f}%" if pd.notna(value) else "자료부족")
    df["위험색상"] = df["재고위험상태"].map({"긴급 부족": "긴급 부족", "부족 주의": "부족 주의"}).fillna("보조 상태")
    threshold = pd.DataFrame({"threshold": [threshold_value]})
    bars = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X("display_readiness_pct:Q", title="재고준비율(%)", scale=alt.Scale(domain=[0, 100])),
            y=alt.Y("product_name:N", title=None, sort=alt.SortField("display_readiness_pct", order="ascending"), axis=alt.Axis(labelLimit=230)),
            color=alt.Color("위험색상:N", legend=None, scale=alt.Scale(domain=["긴급 부족", "부족 주의", "보조 상태"], range=["#dc2626", "#f59e0b", "#9ca3af"])),
            tooltip=[
                alt.Tooltip("product_code:N", title="제품코드"), alt.Tooltip("product_name:N", title="제품명"),
                alt.Tooltip("재고위험상태:N", title="위험상태"), alt.Tooltip("current_stock_qty:Q", title="현재재고수량", format=",.0f"),
                alt.Tooltip("display_readiness_pct:Q", title="준비율", format=".1f"),
                alt.Tooltip("display_remaining_demand_qty:Q", title="잔여예상수요", format=",.0f"),
                alt.Tooltip("display_shortage_qty:Q", title="부족수량", format=",.0f"),
                alt.Tooltip("display_shortage_amt:Q", title="부족금액", format=",.0f"),
                alt.Tooltip("주요매입처명:N", title="주요매입처명"),
                alt.Tooltip("수요급증여부:N", title="수요급증"), alt.Tooltip("위험보정기준:N", title="위험보정기준"),
            ],
        )
    )
    rule = alt.Chart(threshold).mark_rule(color="#0f766e", strokeDash=[4, 4]).encode(x="threshold:Q")
    labels = alt.Chart(df).mark_text(align="left", dx=4, color="#374151", fontSize=11).encode(
        x="display_readiness_pct:Q", y=alt.Y("product_name:N", sort=alt.SortField("display_readiness_pct", order="ascending")), text="display_readiness_label:N"
    )
    return (bars + rule + labels).properties(height=max(260, min(360, len(df) * 29)))


def _render_stock_chart(facts: dict[str, Any]) -> None:
    threshold_value = float((facts.get("stock_readiness") or {}).get("threshold_pct") or 98.0)
    threshold_text = _fmt_threshold_pct(threshold_value)
    chart = _build_stock_readiness_chart(facts)
    if chart is None:
        st.info(f"준비율 경고기준 {threshold_text}% 미만 재고준비율 조치 대상이 없습니다.")
        return
    st.altair_chart(chart, width="stretch")


def _stock_risk_display_summary(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = (facts.get("inventory") or {}).get("stock_risk_summary") or []
    by_status = {str(row.get("재고위험상태") or ""): row for row in rows if isinstance(row, dict)}
    vendor = (facts.get("inventory") or {}).get("vendor_stock_risk_summary") or {}
    overstock = (facts.get("inventory") or {}).get("stock_overstock_summary") or {}
    return {
        "긴급 부족": {"count": int((by_status.get("긴급 부족") or {}).get("품목수") or 0), "amount": float((by_status.get("긴급 부족") or {}).get("부족예상금액") or 0)},
        "부족 주의": {"count": int((by_status.get("부족 주의") or {}).get("품목수") or 0), "amount": float((by_status.get("부족 주의") or {}).get("부족예상금액") or 0)},
        "과잉 후보": {"count": int(overstock.get("품목수") or 0), "amount": float(overstock.get("과잉후보금액") or 0)},
        "최근 매입 없음": {"count": int(vendor.get("recent_purchase_none_rows") or 0), "amount": float(vendor.get("recent_purchase_none_amount") or 0)},
        "적정": {"count": int((by_status.get("적정") or {}).get("품목수") or 0)},
        "판정 제외": {"count": int((by_status.get("판정 제외") or {}).get("품목수") or 0)},
        "매입처 미확인": {"count": int(vendor.get("vendor_unknown_rows") or 0), "amount": float(vendor.get("vendor_unknown_amount") or 0)},
    }


def _build_count_donut(rows: list[dict[str, Any]], *, total_label: str, total: int, colors: list[str]) -> alt.Chart | None:
    frame = pd.DataFrame([row for row in rows if int(row.get("count") or 0) > 0])
    if frame.empty:
        return None
    return alt.Chart(frame).mark_arc(innerRadius=54, outerRadius=82).encode(
        theta=alt.Theta("count:Q"),
        color=alt.Color("label:N", legend=alt.Legend(orient="bottom", title=None), scale=alt.Scale(domain=[row["label"] for row in rows], range=colors)),
        tooltip=[alt.Tooltip("label:N", title="상태"), alt.Tooltip("count:Q", title="품목 수", format=",.0f"), alt.Tooltip("amount:Q", title="금액", format=",.0f")],
    ).properties(title={"text": total_label, "subtitle": [f"{total:,}개"]}, height=245)


def _demand_surge_presentation_state(facts: dict[str, Any]) -> dict[str, Any]:
    """Derive display-only surge partitions from the persisted aggregate facts."""
    summary = (facts.get("inventory") or {}).get("stock_demand_surge_summary") or {}
    total = int(summary.get("전체수요급증품목수", summary.get("품목수", 0)) or 0)
    top_rows = [
        {"label": "기존 예상 초과", "count": int(summary.get("기존예상초과품목수") or 0), "amount": 0.0},
        {"label": "예상외 출고 발생", "count": int(summary.get("예상외출고발생품목수") or 0), "amount": 0.0},
    ]
    detail_rows = [
        {"label": "예상 누락", "count": int(summary.get("예상누락품목수") or 0)},
        {"label": "계절성 재발생 후보", "count": int(summary.get("계절성재발생후보품목수") or 0)},
        {"label": "3개월 이상 재출고", "count": int(summary.get("3개월이상재출고품목수") or 0)},
        {"label": "신규 출고 후보", "count": int(summary.get("신규출고후보품목수") or 0)},
        {"label": "분류자료부족", "count": int(summary.get("분류자료부족품목수") or 0)},
    ]
    unexpected_total = int(summary.get("예상외출고발생품목수") or 0)
    return {
        "total": total,
        "top_rows": top_rows,
        "detail_rows": detail_rows,
        "top_partition_valid": total >= 0 and sum(int(row["count"]) for row in top_rows) == total,
        "detail_partition_valid": unexpected_total >= 0 and sum(int(row["count"]) for row in detail_rows) == unexpected_total,
        "unexpected_total": unexpected_total,
    }


def _build_demand_surge_type_chart(rows: list[dict[str, Any]]) -> alt.Chart | None:
    """Build a compact count-only chart for existing surge detail classifications."""
    frame = pd.DataFrame([row for row in rows if int(row.get("count") or 0) > 0])
    if frame.empty:
        return None
    frame = frame.sort_values(["count", "label"], ascending=[False, True], kind="stable")
    frame["count_label"] = frame["count"].map(lambda value: f"{int(value):,}개")
    order = frame["label"].tolist()
    colors = {
        "예상 누락": "#64748b",
        "계절성 재발생 후보": "#0f766e",
        "3개월 이상 재출고": "#2563eb",
        "신규 출고 후보": "#7c3aed",
        "분류자료부족": "#9ca3af",
    }
    y = alt.Y("label:N", title=None, sort=order, axis=alt.Axis(labelLimit=180))
    bars = alt.Chart(frame).mark_bar(cornerRadiusEnd=3).encode(
        x=alt.X("count:Q", title="품목 수", axis=alt.Axis(tickMinStep=1)),
        y=y,
        color=alt.Color("label:N", legend=None, scale=alt.Scale(domain=list(colors), range=list(colors.values()))),
        tooltip=[alt.Tooltip("label:N", title="유형"), alt.Tooltip("count:Q", title="품목 수", format=",.0f")],
    )
    labels = alt.Chart(frame).mark_text(align="left", dx=5, color="#334155").encode(
        x=alt.X("count:Q"), y=y, text="count_label:N"
    )
    return (bars + labels).properties(height=max(150, min(260, len(frame) * 38)))


def _render_stock_risk_summary(facts: dict[str, Any]) -> None:
    summary = _stock_risk_display_summary(facts)
    amount_unit = _facts_amount_display_unit(facts)
    statuses = ["긴급 부족", "부족 주의", "적정", "판정 제외"]
    donut_rows = [{"label": label, "count": summary[label]["count"], "amount": summary[label].get("amount", 0.0)} for label in statuses]
    total = sum(int(row["count"]) for row in donut_rows)
    st.markdown("### 재고 위험 구성")
    donut = _build_count_donut(donut_rows, total_label="판정 대상", total=total, colors=["#dc2626", "#f59e0b", "#16a34a", "#9ca3af"])
    if donut is not None:
        st.altair_chart(donut, width="stretch")
    for label in statuses:
        item = summary[label]
        suffix = f" / {_fmt_dashboard_amount(item.get('amount', 0), amount_unit)}" if label in {"긴급 부족", "부족 주의"} else ""
        st.caption(f"{label} {item['count']:,}개{suffix}")
    st.caption(f"과잉 후보 {summary['과잉 후보']['count']:,}개 / {_fmt_dashboard_amount(summary['과잉 후보']['amount'], amount_unit)}")
    st.caption(f"최근 매입 없음 {summary['최근 매입 없음']['count']:,}개 / {_fmt_dashboard_amount(summary['최근 매입 없음']['amount'], amount_unit)}")
    st.caption(f"매입처 미확인 {summary['매입처 미확인']['count']:,}개")
    st.caption("과잉 후보는 적정 품목의 보조 관찰지표이며 기본 재고위험 합계에는 중복 반영하지 않습니다.")
    demand_surge = (facts.get("inventory") or {}).get("stock_demand_surge_summary") or {}
    if int(demand_surge.get("품목수") or 0) > 0:
        st.caption(f"참고: 수요급증 {int(demand_surge.get('품목수') or 0)}개 품목은 현재 출고속도를 반영해 평가월 잔여수요를 다시 계산했습니다.")


def _render_vendor_stock_risk(facts: dict[str, Any]) -> None:
    inventory = facts.get("inventory") or {}
    summary = inventory.get("vendor_stock_risk_summary") or {}
    rows = inventory.get("vendor_stock_risk_top_rows") or []
    if not summary:
        return
    amount_unit = _facts_amount_display_unit(facts)
    total_amount = float(summary.get("total_adjusted_shortage_amount") or 0)
    if amount_unit == "auto":
        divisor, _ = _amount_display_spec("auto", abs(total_amount))
        amount_unit = "million" if divisor == 1_000_000 else ("thousand" if divisor == 1_000 else "won")

    st.markdown("### 매입처별 재고위험 TOP 5")
    st.caption("최근 완료월 매입 자료로 선정한 주요 매입처별 위험금액입니다.")
    supply_col, chart_col = st.columns([35, 65])
    with supply_col:
        st.markdown("#### 공급 연결 상태")
        supply_rows = [
            {"label": "정상 귀속 위험", "count": int(summary.get("assigned_rows") or 0), "amount": float(summary.get("assigned_adjusted_shortage_amount") or 0)},
            {"label": "최근 매입 없음", "count": int(summary.get("recent_purchase_none_rows") or 0), "amount": float(summary.get("recent_purchase_none_amount") or 0)},
            {"label": "매입처 미확인", "count": int(summary.get("vendor_unknown_rows") or 0), "amount": float(summary.get("vendor_unknown_amount") or 0)},
        ]
        supply_total = sum(row["count"] for row in supply_rows)
        supply_donut = _build_count_donut(supply_rows, total_label="공급연결 판정", total=supply_total, colors=["#0f766e", "#f59e0b", "#9ca3af"])
        if supply_total == int(summary.get("risk_rows") or 0) and supply_donut is not None:
            st.altair_chart(supply_donut, width="stretch")
        else:
            st.caption("공급 연결 상태 집계가 중복되거나 불완전해 도넛으로 표시하지 않았습니다.")
        for row in supply_rows:
            st.caption(f"{row['label']} {row['count']:,}개 / {_fmt_dashboard_amount(row['amount'], amount_unit)}")

    if not rows:
        with chart_col:
            st.caption("정상 귀속된 긴급 부족·부족 주의 매입처가 없습니다.")
        return
    frame = pd.DataFrame(rows).head(5).copy()
    if frame.empty:
        return
    divisor, unit_label = _amount_display_spec(amount_unit, total_amount)
    frame["표시매입처"] = frame.apply(
        lambda row: str(row.get("주요매입처명") or "").strip()
        if str(row.get("주요매입처명") or "").strip() and frame["주요매입처명"].astype(str).eq(str(row.get("주요매입처명") or "")).sum() == 1
        else f"{str(row.get('주요매입처명') or '').strip() or '미확인'} [{str(row.get('주요매입처코드') or '').strip()}]",
        axis=1,
    )
    order = frame["표시매입처"].tolist()
    chart_rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        for label, key, color in (("긴급 부족금액", "긴급부족금액", "#dc2626"), ("부족 주의 금액", "부족주의금액", "#f59e0b")):
            chart_rows.append({
                "표시매입처": row["표시매입처"], "구분": label, "금액": float(row.get(key) or 0) / divisor,
                "전체위험금액": float(row.get("전체위험보정부족금액") or 0), "긴급부족품목수": int(row.get("긴급부족품목수") or 0),
                "부족주의품목수": int(row.get("부족주의품목수") or 0), "위험품목수": int(row.get("위험품목수") or 0),
                "수요급증품목수": int(row.get("수요급증품목수") or 0), "주요매입처코드": str(row.get("주요매입처코드") or ""),
            })
    chart_df = pd.DataFrame(chart_rows)
    chart = alt.Chart(chart_df).mark_bar().encode(
        y=alt.Y("표시매입처:N", sort=order, title=None),
        x=alt.X("금액:Q", stack="zero", title=f"위험보정 부족금액 ({unit_label})", axis=alt.Axis(format=",.0f")),
        color=alt.Color("구분:N", scale=alt.Scale(domain=["긴급 부족금액", "부족 주의 금액"], range=["#dc2626", "#f59e0b"])),
        tooltip=[
            alt.Tooltip("표시매입처:N", title="주요 매입처"), alt.Tooltip("주요매입처코드:N", title="코드"),
            alt.Tooltip("긴급부족품목수:Q", title="긴급 부족 품목", format=",.0f"), alt.Tooltip("부족주의품목수:Q", title="부족 주의 품목", format=",.0f"),
            alt.Tooltip("위험품목수:Q", title="위험 품목", format=",.0f"), alt.Tooltip("수요급증품목수:Q", title="수요급증 품목", format=",.0f"),
            alt.Tooltip("전체위험금액:Q", title="전체 위험금액", format=",.0f"), alt.Tooltip("금액:Q", title="구분 금액", format=",.0f"),
        ],
    ).properties(height=max(180, min(360, len(order) * 30)))
    with chart_col:
        st.altair_chart(chart, width="stretch")
        st.caption("주요 매입처는 최근 완료월의 순매입금액·순입고수량·최근 매입일 순으로 선정합니다.")


def _render_demand_surge_detail_summary(facts: dict[str, Any]) -> None:
    """Render the persisted demand-surge partitions without querying new rows."""
    summary = (facts.get("inventory") or {}).get("stock_demand_surge_summary") or {}
    state = _demand_surge_presentation_state(facts)
    total = int(state["total"])
    if total <= 0:
        return

    st.markdown("### 수요급증 세부")
    composition_col, type_col = st.columns([35, 65])
    with composition_col:
        st.markdown("#### 수요급증 구성")
        if state["top_partition_valid"]:
            donut = _build_count_donut(
                state["top_rows"], total_label="수요급증", total=total, colors=["#f97316", "#7c3aed"]
            )
            if donut is not None:
                st.altair_chart(donut, width="stretch")
            for row in state["top_rows"]:
                pct = (int(row["count"]) / total * 100.0) if total else 0.0
                st.caption(f"{row['label']} {int(row['count']):,}개 / {_fmt_number(pct, 1)}%")
        else:
            st.caption("상위 분류 합계가 전체 수요급증과 일치하지 않아 독립 지표로 표시합니다.")
            for row in state["top_rows"]:
                _metric_card(row["label"], row["count"], "개")

    with type_col:
        st.markdown("#### 예상외 출고 유형")
        if state["detail_partition_valid"]:
            chart = _build_demand_surge_type_chart(state["detail_rows"])
            if chart is not None:
                st.altair_chart(chart, width="stretch")
            else:
                st.caption("예상외 출고 발생 품목이 없습니다.")
        else:
            st.caption("하위 유형 합계가 예상외 출고 발생과 일치하지 않아 독립 지표로 표시합니다.")
            for row in state["detail_rows"]:
                _metric_card(row["label"], row["count"], "개")

    with st.expander("분류 기준 보기"):
        st.caption("예상 누락: 최근 3개월 완료월 출고 이력이 있으나 당월 기준예상은 0인 품목입니다.")
        st.caption("계절성 재발생 후보: 최근 3개월 무출고이면서 전년 동월 ±1개월에 양의 순출고 이력이 있는 품목입니다.")
        st.caption("3개월 이상 재출고: 최근 3개월 무출고 후 지원기간 과거 양의 순출고 이력이 있는 품목입니다.")
        start_month = str(summary.get("이력지원시작월") or "")
        end_month = str(summary.get("이력지원종료월") or "")
        if start_month and end_month:
            st.caption(f"신규 출고 후보: 지원기간 {start_month}~{end_month} 완료월에 양의 순출고 이력이 없는 품목이며, ERP 전체 최초 출고 확정은 아닙니다.")
        st.caption("분류자료부족: 제품코드 또는 과거 출고 이력이 부족해 세부 유형을 확정할 수 없는 품목입니다.")


def _render_turnover(facts: dict[str, Any]) -> None:
    turnover = facts.get("turnover_days") or {}
    st.caption("매입/매출 거래 회전일")
    st.info(
        "v0.1에서는 최근 90일 정상 매입/매출 고유 거래일 facts가 아직 연결되지 않아 자료부족으로 표시합니다. "
        "입금/출금/현금 회전일은 원천자료가 없어 표시하지 않습니다."
    )
    if turnover.get("definition"):
        st.caption(str(turnover.get("definition")))


def _dashboard_drilldown_params(cache: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
    """Build the small, code-only handoff payload for an existing SIMS view."""
    source = dict(cache.get("params") or {})
    allowed = (
        "month_from", "month_to", "evaluation_month", "stock_mode", "stock_cd_list",
        "vendor_group_list", "vendor_kind_list", "product_group_list", "product_di_list",
        "product_class_list", "io_gu_list", "amount_display_unit", "product_supplier_scope_mode",
        "manufacturer_codes", "manufacturer_manager_codes", "order_vendor_codes", "purchase_manager_codes",
    )
    params = {key: source.get(key) for key in allowed if source.get(key) not in (None, "", [])}
    params.update(dict(action.get("drilldown_params") or {}))
    return params


def _build_dashboard_drilldown_request(cache: dict[str, Any], action: dict[str, Any]) -> dict[str, Any] | None:
    """Build a compact request while rendering; the button callback only stores it."""
    target_action = str(action.get("drilldown_action") or action.get("drill_down") or "").strip()
    target_code = str(action.get("target_code") or action.get("product_code") or "").strip()
    room_id = get_current_chat_room_id()
    company_id = str(cache.get("company_id") or "").strip()
    event_id = str(cache.get("dashboard_event_id") or "").strip()
    if not target_action or not target_code or not room_id or not company_id or not event_id:
        return None
    params = _dashboard_drilldown_params(cache, action)
    params["product_code"] = target_code
    return {
        "request_token": str(uuid.uuid4()),
        "source": "dashboard",
        "source_dashboard_event_id": event_id,
        "source_room_id": room_id,
        "company_id": company_id,
        "target_category": "분석/KPI",
        "target_action": target_action,
        "target_params": params,
        "created_reason": str(action.get("cause_type") or ""),
        "consume_once": True,
    }


def _queue_dashboard_drilldown_request(request: dict[str, Any] | None) -> None:
    """Streamlit button callback: queue one validated request and nothing else."""
    if not isinstance(request, dict):
        return
    target_action = str(request.get("target_action") or "").strip()
    target_params = request.get("target_params") or {}
    target_code = str(target_params.get("product_code") or "").strip() if isinstance(target_params, dict) else ""
    required = (
        request.get("request_token"), request.get("source_dashboard_event_id"),
        request.get("source_room_id"), request.get("company_id"), target_action, target_code,
    )
    if not all(str(value or "").strip() for value in required):
        return
    st.session_state["__dashboard_drilldown_request"] = dict(request)
    log.info(
        "[dashboard.drilldown.request] stage=callback_queued request_token_present=True "
        "source_event_present=True source_room_present=True target_action=%s target_code_present=True",
        target_action,
    )


def _dashboard_action_detail_selection(cache: dict[str, Any], action: dict[str, Any]) -> dict[str, str] | None:
    """Build a local-only selection for the active Dashboard cache."""
    room_id = get_current_chat_room_id()
    company_id = str(cache.get("company_id") or "").strip()
    event_id = str(cache.get("dashboard_event_id") or "").strip()
    action_id = str(action.get("action_id") or "").strip()
    product_code = str(action.get("target_code") or action.get("product_code") or "").strip()
    if not all((room_id, company_id, event_id, action_id, product_code)):
        return None
    return {
        "room_id": room_id,
        "company_id": company_id,
        "dashboard_event_id": event_id,
        "action_id": action_id,
        "product_code": product_code,
        "risk_detail_instance_key": _risk_detail_instance_key(cache),
    }


def _request_dashboard_scroll_suppression(reason: str) -> None:
    """Suppress one existing chat-bottom scroll for a local Dashboard interaction."""
    st.session_state["__dashboard_lite_suppress_chat_autoscroll_once"] = {"reason": str(reason or "local_filter")}
    log.info(
        "[dashboard.scroll] reason=%s suppress_requested=True suppress_consumed=False message_appended=False scroll_to_bottom=False",
        str(reason or "local_filter"),
    )


def _select_dashboard_action_detail(selection: dict[str, str] | None) -> None:
    """Callback only: select an existing cached row and request its local detail view."""
    if not isinstance(selection, dict):
        return
    required = ("room_id", "company_id", "dashboard_event_id", "action_id", "product_code", "risk_detail_instance_key")
    if not all(str(selection.get(key) or "").strip() for key in required):
        return
    st.session_state["__dashboard_selected_action_detail"] = dict(selection)
    st.session_state[f"__dashboard_lite_risk_detail_open_request::{selection['risk_detail_instance_key']}"] = {
        "action_id": str(selection["action_id"]),
        "product_code": str(selection["product_code"]),
    }
    _request_dashboard_scroll_suppression("action_detail")
    log.info(
        "[dashboard.action_detail] stage=callback_selected room_match=True company_match=True event_match=True "
        "action_id_present=True product_code_present=True detail_match_count=0 db_query_count=0 chat_push_count=0 suppress_autoscroll=True"
    )


def _safe_action_rank(action: dict[str, Any], fallback_index: int) -> int:
    """Read current numeric priorities and legacy rank-based Dashboard actions safely."""
    for key in ("priority", "rank"):
        value = action.get(key)
        if isinstance(value, bool):
            continue
        try:
            if value not in (None, ""):
                return int(str(value).strip())
        except (TypeError, ValueError):
            continue
    return int(fallback_index)


def _legacy_action_status(action: dict[str, Any]) -> str:
    status = str(action.get("status") or action.get("risk_grade") or "").strip()
    if status:
        return status
    priority = action.get("priority")
    if isinstance(priority, str) and priority.strip() and not priority.strip().isdigit():
        return priority.strip()
    return "-"


def _legacy_action_target(action: dict[str, Any]) -> str:
    return str(
        action.get("target_name") or action.get("product_name") or action.get("target")
        or action.get("product_code") or "-"
    )


def _render_today_actions(facts: dict[str, Any], cache: dict[str, Any], *, render_mode: str) -> None:
    actions = facts.get("today_actions") or []
    st.caption("상태 · 근거 · 판정 기준 · 권장 조치 순서로 최대 10건을 표시합니다.")
    if not actions:
        st.success("현재 기본 규칙으로 조치가 필요한 항목이 없습니다.")
        return
    interactive = render_mode == "primary"
    event_id = str(cache.get("dashboard_event_id") or "")
    amount_unit = _facts_amount_display_unit(facts)
    headers = st.columns([0.5, 1.1, 1.8, 2.2, 2.0, 2.0, 1.0])
    for column, label in zip(headers, ("순위", "상태", "대상", "판단 근거", "판정 기준", "권장 조치", "상세")):
        with column:
            st.caption(label)
    for fallback_index, action in enumerate(actions[:10], start=1):
        if not isinstance(action, dict):
            action = {}
        cols = st.columns([0.5, 1.1, 1.8, 2.2, 2.0, 2.0, 1.0])
        with cols[0]:
            st.markdown(f"**{_safe_action_rank(action, fallback_index)}**")
        with cols[1]:
            st.write(_legacy_action_status(action))
        with cols[2]:
            st.write(_legacy_action_target(action))
        with cols[3]:
            value = action.get("evidence_value")
            unit = str(action.get("evidence_unit") or "")
            evidence = str(action.get("evidence_label") or "")
            if value is not None and unit == "원":
                evidence = f"{evidence} {_fmt_dashboard_amount(value, amount_unit)}"
            if not evidence and action.get("shortage_amt") not in (None, ""):
                evidence = _fmt_dashboard_amount(action.get("shortage_amt"), amount_unit)
            st.caption(evidence or str(action.get("evidence") or "-"))
        with cols[4]:
            threshold_label = str(action.get("threshold_label") or "")
            threshold_value = action.get("threshold_value")
            cause_type = str(action.get("cause_type") or "")
            if threshold_value is None and action.get("stock_readiness_pct") not in (None, ""):
                threshold = f"재고준비율 {_fmt_threshold_pct(action.get('stock_readiness_pct'))}%"
            elif threshold_value is None:
                threshold = threshold_label or "-"
            elif cause_type in {"stock_shortage", "sales_decline"}:
                threshold = f"{threshold_label} {_fmt_threshold_pct(threshold_value)}%"
            elif cause_type == "overstock_candidate":
                threshold = f"{threshold_label} {_fmt_number(threshold_value)}"
            else:
                threshold = f"{threshold_label} {_fmt_number(threshold_value)}".strip()
            st.caption(threshold)
        with cols[5]:
            st.caption(str(action.get("recommended_action") or "-"))
        with cols[6]:
            selection = _dashboard_action_detail_selection(cache, action) if interactive else None
            if interactive and selection and callable(getattr(st, "button", None)):
                action_id = str(selection["action_id"])
                st.button(
                    "상세 보기",
                    key=f"__dashboard_lite_action_drilldown::{event_id}::{action_id}",
                    on_click=_select_dashboard_action_detail,
                    args=(selection,),
                )
            elif not interactive and bool(action.get("drilldown_action") or action.get("drill_down")):
                st.caption("상세는 현재 조회에서만 가능")


def _risk_detail_instance_key(cache: dict[str, Any]) -> str:
    params = dict(cache.get("params") or {})
    room_id = get_current_chat_room_id()
    source = "|".join(
        (
            str(cache.get("company_id") or ""),
            room_id,
            str(cache.get("cache_key") or cache.get("query_fingerprint") or ""),
            str(params.get("month_from") or ""),
            str(params.get("month_to") or ""),
            str(params.get("evaluation_month") or ""),
            repr(supplier_scope_fingerprint(params)),
        )
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]


def _selected_dashboard_action_detail(cache: dict[str, Any], *, render_mode: str) -> dict[str, str] | None:
    """Return only a selection owned by the current primary Dashboard result."""
    selected = st.session_state.get("__dashboard_selected_action_detail")
    if render_mode != "primary" or not isinstance(selected, dict):
        return None
    room_id = get_current_chat_room_id()
    matches = {
        "room_match": bool(room_id and room_id == str(selected.get("room_id") or "") == str(cache.get("room_id") or "")),
        "company_match": bool(str(selected.get("company_id") or "") and str(selected.get("company_id") or "") == str(cache.get("company_id") or "")),
        "event_match": bool(str(selected.get("dashboard_event_id") or "") and str(selected.get("dashboard_event_id") or "") == str(cache.get("dashboard_event_id") or "")),
    }
    action_id_present = bool(str(selected.get("action_id") or "").strip())
    product_code_present = bool(str(selected.get("product_code") or "").strip())
    if not (all(matches.values()) and action_id_present and product_code_present):
        st.session_state.pop("__dashboard_selected_action_detail", None)
        log.info(
            "[dashboard.action_detail] stage=discarded room_match=%s company_match=%s event_match=%s "
            "action_id_present=%s product_code_present=%s detail_match_count=0 db_query_count=0 chat_push_count=0 suppress_autoscroll=False",
            matches["room_match"], matches["company_match"], matches["event_match"], action_id_present, product_code_present,
        )
        return None
    return {key: str(value or "") for key, value in selected.items()}


def _render_selected_dashboard_action_detail(facts: dict[str, Any], cache: dict[str, Any], *, render_mode: str) -> None:
    """Render selected action rows from the current in-memory Dashboard facts only."""
    selected = _selected_dashboard_action_detail(cache, render_mode=render_mode)
    if not selected:
        return
    rows = list(((facts.get("inventory") or {}).get("risk_detail_rows") or []))
    product_code = str(selected["product_code"]).strip()
    matched_rows = [row for row in rows if isinstance(row, dict) and str(row.get("제품코드") or "").strip() == product_code]
    log.info(
        "[dashboard.action_detail] stage=rendered room_match=True company_match=True event_match=True "
        "action_id_present=True product_code_present=True detail_match_count=%s db_query_count=0 chat_push_count=0 suppress_autoscroll=False",
        len(matched_rows),
    )
    st.markdown("#### 선택 조치 상세")
    if not matched_rows:
        st.info("현재 Dashboard 상세자료에서 해당 품목을 찾지 못했습니다.")
        return
    display_columns = [
        "제품코드", "제품명", "위험상태", "위험사유", "현재재고수량", "위험보정잔여예상수요",
        "위험보정부족예상수량", "위험보정부족예상금액", "위험보정재고준비율", "주요매입처명",
        "수요급증여부", "위험보정기준", "권장 조치",
    ]
    display = pd.DataFrame(matched_rows)
    display = display[[column for column in display_columns if column in display.columns]]
    st.dataframe(display, width="stretch", hide_index=True)


def _clear_stale_risk_detail_export_cache(instance_key: str) -> None:
    prefix = "__dashboard_lite_risk_detail_excel::"
    for key in list(st.session_state.keys()):
        if str(key).startswith(prefix) and not str(key).endswith(instance_key):
            st.session_state.pop(key, None)


def _risk_detail_vendor_options(rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, str]]:
    labels = {"전체": "전체", "recent_purchase_none": "최근 매입 없음", "vendor_unknown": "매입처 미확인"}
    for row in rows:
        key = str(row.get("_주요매입처필터키") or "")
        if not key.startswith("assigned:"):
            continue
        code = str(row.get("주요매입처코드") or "").strip()
        name = str(row.get("주요매입처명") or "").strip() or code
        if code:
            labels[key] = f"{name} [{code}]"
    assigned_keys = sorted(key for key in labels if key.startswith("assigned:"))
    options = ["전체", *assigned_keys]
    if any(str(row.get("_주요매입처필터키") or "") == "recent_purchase_none" for row in rows):
        options.append("recent_purchase_none")
    if any(str(row.get("_주요매입처필터키") or "") == "vendor_unknown" for row in rows):
        options.append("vendor_unknown")
    return options, labels


def _risk_detail_query_conditions(
    cache: dict[str, Any],
    facts: dict[str, Any],
    *,
    excel_created_at: str,
) -> list[dict[str, str]]:
    params = dict(cache.get("params") or {})
    inventory = facts.get("inventory") or {}
    stock_codes = _clean_list(params.get("stock_cd_list"))
    stock_names = _clean_list(params.get("stock_name_list"))
    stock_labels = [
        f"{stock_names[index]} [{code}]" if index < len(stock_names) and stock_names[index] else code
        for index, code in enumerate(stock_codes)
    ]
    stock_mode = "장부재고" if str(params.get("stock_mode") or "") == "book" else "실재고"
    threshold = _fmt_threshold_pct((facts.get("stock_readiness") or {}).get("threshold_pct") or params.get("readiness_warning_pct") or 98)
    vendor_summary = inventory.get("vendor_stock_risk_summary") or {}
    scope = normalize_product_supplier_scope(params)
    mode = scope["product_supplier_scope_mode"]
    supplier_conditions = [{"조건명": "공급 기준", "값": {SCOPE_MANUFACTURER: "제약사", SCOPE_ORDER_VENDOR: "발주처"}[mode]}]
    if mode == SCOPE_MANUFACTURER:
        supplier_conditions.extend([
            {"조건명": "제약사", "값": str(params.get("supplier_scope_label") or "전체")},
            {"조건명": "제약사 담당자", "값": str(params.get("supplier_manager_label") or "전체")},
        ])
    elif mode == SCOPE_ORDER_VENDOR:
        supplier_conditions.extend([
            {"조건명": "발주처", "값": str(params.get("supplier_scope_label") or "전체")},
            {"조건명": "발주담당자", "값": str(params.get("supplier_manager_label") or "전체")},
        ])
    return [
        {"조건명": "시작월", "값": str(params.get("month_from") or "")},
        {"조건명": "종료월", "값": str(params.get("month_to") or "")},
        {"조건명": "평가월", "값": str(params.get("evaluation_month") or "")},
        *supplier_conditions,
        {"조건명": "재고기준", "값": stock_mode},
        {"조건명": "대상 재고위치", "값": ", ".join(stock_labels) if stock_labels else "전체"},
        {"조건명": "재고준비율 경고기준", "값": f"{threshold}%"},
        {"조건명": "주요매입처 기준 시작월", "값": str(vendor_summary.get("basis_month_from") or "")},
        {"조건명": "주요매입처 기준 종료월", "값": str(vendor_summary.get("basis_month_to") or "")},
        {"조건명": "주요매입처 기준", "값": "최근 6완료월"},
        {"조건명": "조회완료시각", "값": str(cache.get("created_at") or "")},
        {"조건명": "Excel생성시각", "값": str(excel_created_at or "")},
    ]


def _render_risk_detail(
    facts: dict[str, Any],
    cache: dict[str, Any],
    *,
    render_mode: str,
) -> None:
    inventory = facts.get("inventory") or {}
    summary = inventory.get("risk_detail_summary") or {}
    rows = inventory.get("risk_detail_rows") or []
    if not summary:
        return
    st.markdown("### 위험 품목 상세")
    summary_cols = st.columns(5)
    for column, (label, key) in zip(summary_cols, (
        ("전체 위험품목", "source_rows"), ("긴급 부족", "emergency_rows"), ("부족 주의", "warning_rows"),
        ("금액 양수", "amount_positive_rows"), ("금액 0", "zero_amount_rows"),
    )):
        with column:
            _metric_card(label, summary.get(key, 0), "개")

    source_rows = int(summary.get("source_rows") or 0)
    detail_rows_available = int(len(rows))
    if render_mode != "primary":
        log.info(
            "[dashboard.risk_detail.render] render_mode=%s source_rows=%s detail_rows_available=%s toggle_rendered=False export_controls_allowed=False",
            render_mode,
            source_rows,
            detail_rows_available,
        )
        st.caption("위험 상세표와 Excel은 현재 Dashboard 조회 세션에서만 사용할 수 있습니다.")
        return
    if not rows:
        log.info(
            "[dashboard.risk_detail.render] render_mode=primary source_rows=%s detail_rows_available=0 toggle_rendered=False export_controls_allowed=False",
            source_rows,
        )
        st.caption("위험 상세표와 Excel은 현재 Dashboard 조회 세션에서만 사용할 수 있습니다.")
        return

    instance_key = _risk_detail_instance_key(cache)
    _clear_stale_risk_detail_export_cache(instance_key)
    toggle = getattr(st, "toggle", None)
    if not callable(toggle):
        log.info(
            "[dashboard.risk_detail.render] render_mode=primary source_rows=%s detail_rows_available=%s toggle_rendered=False export_controls_allowed=False",
            source_rows,
            detail_rows_available,
        )
        return
    toggle_key = f"__dashboard_lite_risk_detail_toggle::{instance_key}"
    search_key = f"__dashboard_lite_risk_detail_search::{instance_key}"
    open_request = st.session_state.pop(f"__dashboard_lite_risk_detail_open_request::{instance_key}", None)
    if isinstance(open_request, dict):
        selected_code = str(open_request.get("product_code") or "").strip()
        st.session_state[toggle_key] = True
        if selected_code:
            st.session_state[search_key] = selected_code
    show_detail = toggle(
        "상세표 보기",
        key=toggle_key,
        on_change=_request_dashboard_scroll_suppression,
        args=("risk_detail_toggle",),
    )
    if not show_detail:
        log.info(
            "[dashboard.risk_detail.render] render_mode=primary source_rows=%s detail_rows_available=%s toggle_rendered=True export_controls_allowed=False",
            source_rows,
            detail_rows_available,
        )
        return
    export_controls_allowed = bool(require_permission("EXPORT_EXCEL", show_error=False))
    log.info(
        "[dashboard.risk_detail.render] render_mode=primary source_rows=%s detail_rows_available=%s toggle_rendered=True export_controls_allowed=%s",
        source_rows,
        detail_rows_available,
        export_controls_allowed,
    )

    vendor_options, vendor_labels = _risk_detail_vendor_options(rows)
    filter_cols = st.columns((1, 2, 1, 1, 2))
    with filter_cols[0]:
        risk_status = st.selectbox("위험상태", ["전체 위험", "긴급 부족", "부족 주의"], key=f"__dashboard_lite_risk_detail_status::{instance_key}", on_change=_request_dashboard_scroll_suppression, args=("local_filter",))
    with filter_cols[1]:
        vendor_key = st.selectbox("주요매입처", vendor_options, format_func=lambda value: vendor_labels.get(value, "매입처 미확인"), key=f"__dashboard_lite_risk_detail_vendor::{instance_key}", on_change=_request_dashboard_scroll_suppression, args=("local_filter",))
    with filter_cols[2]:
        surge_filter = st.selectbox("수요급증", ["전체", "수요급증", "일반"], key=f"__dashboard_lite_risk_detail_surge::{instance_key}", on_change=_request_dashboard_scroll_suppression, args=("local_filter",))
    with filter_cols[3]:
        include_zero_amount = st.toggle("금액 0 포함", value=True, key=f"__dashboard_lite_risk_detail_zero::{instance_key}", on_change=_request_dashboard_scroll_suppression, args=("local_filter",))
    with filter_cols[4]:
        search_text = st.text_input("제품 검색", key=search_key, on_change=_request_dashboard_scroll_suppression, args=("local_filter",))

    filtered, filter_summary, elapsed_ms = filter_dashboard_risk_detail_rows(
        rows,
        risk_status=risk_status,
        vendor_key=vendor_key,
        surge_filter=surge_filter,
        include_zero_amount=include_zero_amount,
        search_text=search_text,
    )
    display_limit = st.selectbox("화면 표시 행 수", [100, 300, 500], index=0, key=f"__dashboard_lite_risk_detail_limit::{instance_key}", on_change=_request_dashboard_scroll_suppression, args=("local_filter",))
    display = filtered.head(int(display_limit)).copy()
    display.insert(0, "순번", range(1, len(display) + 1))
    display_columns = [
        "순번", "위험상태", "위험사유", "제품코드", "제품명", "규격", "제조사명", "주요매입처명", "현재재고수량",
        "위험보정잔여예상수요", "위험보정부족예상수량", "위험보정부족예상금액", "위험보정재고준비율", "수요급증세부분류",
        "최근 정상 입고일", "입고 경과일", "정상 입고 거래일수", "평균 입고간격일", "입고 자료상태", "입고 지연후보",
        "최근입고 대표매입처명", "최근입고 대표매입처코드", "최근입고 대표매입처출처", "최근365일 입고이력",
    ]
    display = display[[column for column in display_columns if column in display.columns]]
    log.info(
        "[dashboard.risk_detail] source_rows=%s filtered_rows=%s displayed_rows=%s emergency_rows=%s warning_rows=%s zero_amount_rows=%s vendor_filter_applied=%s surge_filter_applied=%s search_filter_applied=%s include_zero_amount=%s display_limit=%s elapsed_ms=%s",
        filter_summary["source_rows"], filter_summary["filtered_rows"], len(display), filter_summary["emergency_rows"], filter_summary["warning_rows"], filter_summary["zero_amount_rows"],
        vendor_key != "전체", surge_filter != "전체", bool(str(search_text or "").strip()), include_zero_amount, display_limit, elapsed_ms,
    )
    st.caption(f"필터 결과 {filter_summary['filtered_rows']:,}건 중 상위 {len(display):,}건 표시")
    st.dataframe(
        display,
        width="stretch",
        height=min(560, max(220, 72 + len(display) * 35)),
        hide_index=True,
        column_config={
            "순번": st.column_config.NumberColumn("순번", format="%d"),
            "현재재고수량": st.column_config.NumberColumn("현재재고수량", format="%,.2f"),
            "위험보정잔여예상수요": st.column_config.NumberColumn("위험보정잔여예상수요", format="%,.2f"),
            "위험보정부족예상수량": st.column_config.NumberColumn("위험보정부족예상수량", format="%,.2f"),
            "위험보정부족예상금액": st.column_config.NumberColumn("위험보정부족예상금액", format="%,.0f"),
            "위험보정재고준비율": st.column_config.NumberColumn("위험보정재고준비율", format="%.2f%%"),
        },
    )

    if not export_controls_allowed:
        log.info(
            "[dashboard.risk_detail_export] permission_allowed=False export_rows=%s vendor_summary_rows=%s",
            len(rows),
            len(inventory.get("vendor_stock_risk_rows") or []),
        )
        st.warning("다운로드 권한이 없습니다. 필요 권한: EXPORT_EXCEL (엑셀/CSV 다운로드)")
        return
    excel_key = f"__dashboard_lite_risk_detail_excel::{instance_key}"
    cache_entry = st.session_state.get(excel_key)
    if not isinstance(cache_entry, dict) or not isinstance(cache_entry.get("bytes"), (bytes, bytearray)):
        if st.button("전체 위험품목 Excel 준비", key=f"__dashboard_lite_risk_detail_prepare::{instance_key}", width="stretch"):
            try:
                bytes_value, export_info = build_dashboard_risk_detail_excel_bytes(
                    rows,
                    inventory.get("vendor_stock_risk_rows") or [],
                    _risk_detail_query_conditions(
                        cache,
                        facts,
                        excel_created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                st.session_state[excel_key] = {"bytes": bytes_value, "export_info": export_info}
                st.rerun()
            except Exception as exc:
                log.warning("[dashboard.risk_detail_export] export_rows=%s vendor_summary_rows=%s sheet_count=0 bytes_size=0 permission_allowed=True elapsed_ms=0 success=False error_type=%s", len(rows), len(inventory.get("vendor_stock_risk_rows") or []), type(exc).__name__)
                st.warning("위험 품목 Excel을 준비하지 못했습니다.")
        return
    st.download_button(
        "위험 품목 Excel 다운로드",
        data=cache_entry["bytes"],
        file_name="dashboard_risk_detail.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"__dashboard_lite_risk_detail_download::{instance_key}",
        width="stretch",
    )


def _dashboard_context_identity() -> dict[str, str]:
    try:
        from app.ui.ssai_login import get_current_user, get_selected_company

        user = get_current_user()
        company = get_selected_company() or {}
        raw_db = str(company.get("db_name") or "")
        db_sig = hashlib.sha256(raw_db.encode("utf-8")).hexdigest()[:12] if raw_db else ""
        return {
            "user_id": str(getattr(user, "user_id", "") or ""),
            "company_id": str(company.get("company_id") or ""),
            "db_sig": db_sig,
        }
    except Exception:
        return {"user_id": "", "company_id": "", "db_sig": ""}


def _apply_saved_dashboard_profile_once() -> None:
    identity = _dashboard_context_identity()
    profile_key = str(identity.get("company_id") or "")
    if not identity.get("company_id"):
        return

    loaded_for = str(st.session_state.get("__dashboard_lite_profile_loaded_for") or "")
    missing_widget_keys = [
        widget_key
        for widget_key in _DASHBOARD_PROFILE_WIDGETS.values()
        if widget_key not in st.session_state
    ]
    if loaded_for == profile_key and not missing_widget_keys:
        log.info(
            "[dashboard.profile_restore] company_id=%s reason=preserve_live_state "
            "profile_found=unknown restored_widget_count=0 skipped_existing_widget_count=%s",
            identity["company_id"],
            len(_DASHBOARD_PROFILE_WIDGETS),
        )
        return

    if not loaded_for:
        restore_reason = "initial_entry"
    elif loaded_for != profile_key:
        restore_reason = "company_change"
    else:
        restore_reason = "action_reentry"

    if restore_reason == "company_change":
        for widget_key in _DASHBOARD_PROFILE_WIDGETS.values():
            st.session_state.pop(widget_key, None)
        missing_widget_keys = list(_DASHBOARD_PROFILE_WIDGETS.values())

    # Manufacturer is deliberately a non-persistent performance test filter.
    # Reset it before applying the shared profile for a fresh Dashboard entry.
    _clear_dashboard_manufacturer_state()
    profile = load_dashboard_profile(company_id=int(identity["company_id"]))
    adapter = build_company_default_adapter(
        profile,
        supported_keys=COMPANY_DEFAULT_KEYS,
    )
    profile_values = dict(adapter.get("effective") or {})
    company_io_key = "__dashboard_lite_company_io_gu_list"
    if isinstance(profile, dict):
        st.session_state[company_io_key] = _clean_list(profile_values.get("io_gu_list"))
    else:
        st.session_state.pop(company_io_key, None)
    log.info(
        "[analysis_profile.adapter] company_id_present=True profile_found=%s target_context=dashboard "
        "supported_key_count=%s applied_default_count=%s explicit_override_count=0 explicit_clear_count=0 "
        "unsupported_key_count=%s cache_used=False reason=%s",
        bool(adapter.get("profile_found")), len(COMPANY_DEFAULT_KEYS), adapter.get("applied_default_count", 0),
        len(adapter.get("unsupported_default_keys") or []), restore_reason,
    )
    restored_widget_count = 0
    skipped_existing_widget_count = 0
    if isinstance(profile, dict):
        for source_key, widget_key in _DASHBOARD_PROFILE_WIDGETS.items():
            if widget_key in st.session_state:
                skipped_existing_widget_count += 1
                continue
            if source_key in profile_values:
                st.session_state[widget_key] = _dashboard_profile_widget_value(source_key, profile_values[source_key])
                restored_widget_count += 1
        io_values = _clean_list(profile_values.get("io_gu_list"))
        log.info(
            "[dashboard.profile_restore] company_id=%s reason=%s profile_found=True "
            "condition_keys=%s io_gu_count=%s io_gu_sample=%s restored_widget_count=%s "
            "skipped_existing_widget_count=%s",
            identity["company_id"], restore_reason, ",".join(sorted(profile_values.keys())), len(io_values), ",".join(io_values[:3]),
            restored_widget_count, skipped_existing_widget_count,
        )
    else:
        log.info(
            "[dashboard.profile_restore] company_id=%s reason=%s profile_found=False "
            "restored_widget_count=0 skipped_existing_widget_count=%s",
            identity["company_id"], restore_reason, len(_DASHBOARD_PROFILE_WIDGETS) - len(missing_widget_keys),
        )
    st.session_state["__dashboard_lite_profile_loaded_for"] = profile_key


def _dashboard_profile_widget_value(source_key: str, value: Any) -> Any:
    """Adapt stored condition values to the concrete Streamlit widget values."""
    if source_key != "io_gu_list":
        return value
    return [
        item if ":" in str(item) else f"0012:{str(item).strip()}"
        for item in _clean_list(value)
    ]


def _dashboard_cache_key(params: dict[str, Any], *, run_seq: int) -> str:
    identity = _dashboard_context_identity()
    payload = {
        "user_id": identity.get("user_id"),
        "company_id": identity.get("company_id"),
        "db_sig": identity.get("db_sig"),
        "month_from": params.get("month_from"),
        "month_to": params.get("month_to"),
        "evaluation_month": params.get("evaluation_month"),
        "source_mode": params.get("source_mode"),
        "stock_mode": params.get("stock_mode"),
        "stock_cd_list": _normalized_key_list(params.get("stock_cd_list")),
        "vendor_group_list": _normalized_key_list(params.get("vendor_group_list")),
        "vendor_kind_list": _normalized_key_list(params.get("vendor_kind_list")),
        "product_group_list": _normalized_key_list(params.get("product_group_list")),
        "product_di_list": _normalized_key_list(params.get("product_di_list")),
        "product_class_list": _normalized_key_list(params.get("product_class_list")),
    "io_gu_list": _normalized_key_list(params.get("io_gu_list")),
        "supplier_scope": supplier_scope_fingerprint(params),
        "major_purchase_vendor_days": params.get("major_purchase_vendor_days"),
        "risk_analysis_days": params.get("risk_analysis_days"),
        "overstock_inactive_days": params.get("overstock_inactive_days"),
        "readiness_warning_pct": params.get("readiness_warning_pct"),
        "risk_quick_view_count": params.get("risk_quick_view_count"),
        "amount_display_unit": params.get("amount_display_unit"),
        "inbound_cycle_days": 365,
        "inbound_vendor_days": 90,
        "inbound_data_cutoff_date": params.get("date_to"),
        "exclude_product_group_list": _normalized_key_list(params.get("exclude_product_group_list")),
        "exclude_product_di_list": _normalized_key_list(params.get("exclude_product_di_list")),
        "exclude_product_class_list": _normalized_key_list(params.get("exclude_product_class_list")),
        "run_seq": int(run_seq or 0),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _dashboard_stock_options() -> tuple[list[str], dict[str, str]]:
    t0 = time.perf_counter()
    options = [opt for opt in _load_stock_code_options() if opt[1]]
    code_to_name = {code: name for _label, code, name in options}
    codes = [code for _label, code, _name in options]
    log.info("[dashboard.option_load] source=stock_options rows=%s elapsed_ms=%s", len(codes), int((time.perf_counter() - t0) * 1000))
    return codes, code_to_name


def _dashboard_code_name_options(gcode: str) -> tuple[list[str], dict[str, str]]:
    t0 = time.perf_counter()
    try:
        fn = getattr(C01, "list_by_group", None)
        if callable(fn):
            df = fn(gcode=gcode, top=2000)
        else:
            df = C01.search_rows(gcode=gcode, top=2000, only_active=True)
        df = df if isinstance(df, pd.DataFrame) else pd.DataFrame(df)
    except Exception:
        df = pd.DataFrame()
    if df.empty:
        log.info("[dashboard.option_load] source=code_name gcode=%s rows=%s elapsed_ms=%s", gcode, 0, int((time.perf_counter() - t0) * 1000))
        return [], {}
    tcol = next((c for c in ("Rd01_Tcode", "항목코드", "상세코드") if c in df.columns), "")
    ncol = next((c for c in ("Rd01_Hnm", "한글명", "코드명") if c in df.columns), "")
    if not tcol or not ncol:
        return [], {}
    work = df[[tcol, ncol]].copy()
    work[tcol] = work[tcol].fillna("").astype(str).str.strip()
    work[ncol] = work[ncol].fillna("").astype(str).str.strip()
    work = work[(work[tcol] != "") & (work[ncol] != "")]
    work = work.drop_duplicates(subset=[tcol], keep="first").sort_values(tcol, kind="stable")
    code_to_name = {f"{gcode}:{str(row[tcol]).strip()}": str(row[ncol]).strip() for _, row in work.iterrows()}
    codes = list(code_to_name.keys())
    log.info("[dashboard.option_load] source=code_name gcode=%s rows=%s elapsed_ms=%s", gcode, len(codes), int((time.perf_counter() - t0) * 1000))
    return codes, code_to_name


def _clear_dashboard_manufacturer_state(*, keep_text: bool = False) -> None:
    """Clear only the non-persistent manufacturer test scope."""
    for key in (
        "__dashboard_lite_manufacturer_test_codes",
        "__dashboard_lite_manufacturer_resolved_code",
        "__dashboard_lite_manufacturer_resolved_name",
        "__dashboard_lite_manufacturer_scope",
        "__dashboard_lite_manufacturer_candidates",
        "__dashboard_lite_manufacturer_candidate_code",
    ):
        st.session_state.pop(key, None)
    if not keep_text:
        st.session_state.pop("__dashboard_lite_manufacturer_text", None)


def _clear_inactive_dashboard_supplier_state(mode: str) -> bool:
    """Clear inactive temporary supplier controls on every mode transition."""
    removed = False
    keys = {
        SCOPE_MANUFACTURER: ("__dashboard_lite_order_vendor_text", "__dashboard_lite_purchase_manager_codes"),
        SCOPE_ORDER_VENDOR: ("__dashboard_lite_manufacturer_text", "__dashboard_lite_manufacturer_manager_codes", "__dashboard_lite_manufacturer_test_codes", "__dashboard_lite_manufacturer_scope"),
    }.get(mode, ())
    for key in keys:
        if st.session_state.get(key):
            removed = True
        st.session_state.pop(key, None)
    return removed


def _on_dashboard_supplier_scope_mode_change() -> None:
    """Immediate, non-querying callback for the temporary supplier scope mode."""
    mode = str(st.session_state.get("__dashboard_lite_product_supplier_scope_mode") or "").strip()
    if mode not in {SCOPE_MANUFACTURER, SCOPE_ORDER_VENDOR}:
        mode = SCOPE_MANUFACTURER
        st.session_state["__dashboard_lite_product_supplier_scope_mode"] = mode
    _clear_inactive_dashboard_supplier_state(mode)


def _dashboard_supplier_manager_options(mode: str) -> list[dict[str, str]]:
    """Cache manager choices by company and scope mode outside Dashboard facts calls."""
    if mode not in {SCOPE_MANUFACTURER, SCOPE_ORDER_VENDOR}:
        return []
    identity = _dashboard_context_identity()
    company_key = str(identity.get("company_id") or "")
    cache = st.session_state.setdefault("__dashboard_lite_supplier_manager_options", {})
    cache_key = f"{company_key}:{mode}"
    if cache_key in cache:
        rows = list(cache.get(cache_key) or [])
        log.info("[dashboard.supplier_manager_options] mode=%s status=success manager_option_count=%s cache_used=True query_elapsed_ms=0", mode, len(rows))
        return rows
    started = time.perf_counter()
    try:
        rows = load_supplier_manager_options(mode=mode)
    except Exception as exc:
        log.warning(
            "[dashboard.supplier_manager_options] mode=%s status=error error_type=%s cache_used=False query_elapsed_ms=%s",
            mode,
            type(exc).__name__,
            int((time.perf_counter() - started) * 1000),
        )
        raise
    cache[cache_key] = rows
    log.info("[dashboard.supplier_manager_options] mode=%s status=success manager_option_count=%s cache_used=False query_elapsed_ms=%s", mode, len(rows), int((time.perf_counter() - started) * 1000))
    return rows


def _resolve_dashboard_supplier(text: Any, *, mode: str) -> dict[str, Any]:
    """Resolve the active vendor family only when Dashboard is submitted."""
    raw = " ".join(str(text or "").split())
    if not raw or raw == "전체":
        return {"status": "all", "codes": [], "names": [], "label": "전체"}
    if len(raw) < 2 and not re.fullmatch(r"[A-Za-z0-9]+", raw):
        return {"status": "too_short", "codes": [], "names": [], "label": "전체"}
    try:
        rows = resolve_supplier_vendor_codes(raw, mode=mode)
    except Exception as exc:
        log.warning("[dashboard.supplier_scope] mode=%s resolver_error_type=%s", mode, type(exc).__name__)
        return {"status": "error", "codes": [], "names": [], "label": "전체"}
    codes = [str(row.get("code") or "") for row in rows if str(row.get("code") or "")]
    names = [str(row.get("name") or "") for row in rows]
    if not codes:
        return {"status": "missing", "codes": [], "names": [], "label": "전체"}
    label = f"{names[0]} [{codes[0]}]" if len(codes) == 1 and names[0] else (f"'{raw}' 포함 {len(codes)}개사" if len(codes) > 1 else codes[0])
    return {"status": "resolved", "codes": codes, "names": names, "label": label}


def _resolve_dashboard_manufacturer(text: Any) -> dict[str, Any]:
    """Resolve an explicit Dashboard-only manufacturer scope at query time."""
    raw = " ".join(str(text or "").split())

    def _scope(status: str, rows: list[dict[str, str]], *, match_mode: str, search_term: str = "") -> dict[str, Any]:
        codes = [str(row.get("code") or "") for row in rows if str(row.get("code") or "")]
        names = [str(row.get("name") or "") for row in rows]
        if match_mode == "all":
            label = "전체"
        elif len(rows) == 1:
            label = f"{names[0]} [{codes[0]}]" if names[0] else codes[0]
        else:
            label = f"'{search_term}' 포함 {len(rows)}개사"
        state = {
            "status": status,
            "codes": codes,
            "names": names,
            "match_mode": match_mode,
            "match_count": len(rows),
            "search_term": search_term,
            "label": label,
        }
        st.session_state["__dashboard_lite_manufacturer_test_codes"] = codes
        st.session_state["__dashboard_lite_manufacturer_resolved_code"] = codes[0] if len(codes) == 1 else ""
        st.session_state["__dashboard_lite_manufacturer_resolved_name"] = names[0] if len(names) == 1 else ""
        st.session_state["__dashboard_lite_manufacturer_scope"] = state
        log.info(
            "[dashboard.manufacturer_scope] match_mode=%s match_count=%s filter_enabled=%s",
            match_mode,
            len(codes),
            bool(codes),
        )
        return state

    if not raw or raw == "전체":
        _clear_dashboard_manufacturer_state(keep_text=True)
        return _scope("all", [], match_mode="all")

    code_match = re.match(r"^\s*([0-9A-Za-z]+)(?:\s*-.*)?$", raw)
    exact_code = code_match.group(1).strip() if code_match else ""

    def _normalize_rows(df: Any) -> list[dict[str, str]]:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return []
        work = df.copy()
        for col in ("manufacturer_code", "manufacturer_name"):
            if col not in work.columns:
                work[col] = ""
            work[col] = work[col].fillna("").astype(str).str.strip()
        work = work[work["manufacturer_code"] != ""].drop_duplicates("manufacturer_code", keep="first")
        return [
            {"code": str(row["manufacturer_code"]), "name": str(row["manufacturer_name"])}
            for _, row in work.iterrows()
        ]

    base_sql = """
        SELECT DISTINCT
            LTRIM(RTRIM(P.Rd04_Ven_Cd)) AS manufacturer_code,
            COALESCE(NULLIF(LTRIM(RTRIM(V.Rd03_Ven_Nm)), ''), LTRIM(RTRIM(P.Rd04_Ven_Cd))) AS manufacturer_name
        FROM dbo.Rddbc040 AS P WITH (NOLOCK)
        LEFT JOIN dbo.Rddbc030 AS V WITH (NOLOCK)
            ON P.Rd04_Ven_Cd = V.Rd03_Ven_Cd
        WHERE LTRIM(RTRIM(P.Rd04_Ven_Cd)) <> ''
    """
    try:
        if exact_code:
            exact_rows = _normalize_rows(query_to_df(
                base_sql + " AND LTRIM(RTRIM(P.Rd04_Ven_Cd)) = ? ORDER BY manufacturer_name, manufacturer_code",
                (exact_code,),
            ))
            if len(exact_rows) == 1:
                _clear_dashboard_manufacturer_state(keep_text=True)
                return _scope("resolved", exact_rows, match_mode="exact", search_term=raw)
            _clear_dashboard_manufacturer_state(keep_text=True)
            return _scope("missing", [], match_mode="exact", search_term=raw)
        if len(raw) < 2:
            _clear_dashboard_manufacturer_state(keep_text=True)
            return _scope("too_short", [], match_mode="name_like", search_term=raw)
        like_value = f"%{raw}%"
        rows = _normalize_rows(query_to_df(
            base_sql + " AND LTRIM(RTRIM(V.Rd03_Ven_Nm)) LIKE ? ORDER BY manufacturer_name, manufacturer_code",
            (like_value,),
        ))
    except Exception as exc:
        _clear_dashboard_manufacturer_state(keep_text=True)
        log.warning("[dashboard.manufacturer_resolve] resolved=False error_type=%s", type(exc).__name__)
        return _scope("error", [], match_mode="error", search_term=raw)

    _clear_dashboard_manufacturer_state(keep_text=True)
    return _scope("resolved" if rows else "missing", rows, match_mode="name_like", search_term=raw)


def _load_dashboard_scope_options() -> dict[str, Any]:
    """Load dashboard condition choices once per session cache."""
    stock_codes, stock_code_to_name = _dashboard_stock_options()
    product_group_codes, product_group_code_to_name = _dashboard_code_name_options("0013")
    product_di_codes, product_di_code_to_name = _dashboard_code_name_options("0004")
    product_class_codes, product_class_code_to_name = _dashboard_code_name_options("0031")
    vendor_group_codes, vendor_group_code_to_name = _dashboard_code_name_options("0019")
    vendor_kind_codes, vendor_kind_code_to_name = _dashboard_code_name_options("0009")
    io_gu_codes, io_gu_code_to_name = _dashboard_code_name_options("0012")
    return {
        "cache_version": DASHBOARD_LITE_OPTION_CACHE_VERSION,
        "stock_codes": stock_codes,
        "stock_code_to_name": stock_code_to_name,
        "product_group_codes": product_group_codes,
        "product_group_code_to_name": product_group_code_to_name,
        "product_di_codes": product_di_codes,
        "product_di_code_to_name": product_di_code_to_name,
        "product_class_codes": product_class_codes,
        "product_class_code_to_name": product_class_code_to_name,
        "vendor_group_codes": vendor_group_codes,
        "vendor_group_code_to_name": vendor_group_code_to_name,
        "vendor_kind_codes": vendor_kind_codes,
        "vendor_kind_code_to_name": vendor_kind_code_to_name,
        "io_gu_codes": io_gu_codes,
        "io_gu_code_to_name": io_gu_code_to_name,
    }


def _render_dashboard_scope_form() -> tuple[bool, bool, dict[str, Any] | None]:
    defaults = default_dashboard_lite_scope()
    _prepare_dashboard_profile_scalar_state()
    option_cache = st.session_state.get(DASHBOARD_LITE_OPTION_CACHE_KEY)
    if (
        not isinstance(option_cache, dict)
        or option_cache.get("cache_version") != DASHBOARD_LITE_OPTION_CACHE_VERSION
    ):
        option_cache = _load_dashboard_scope_options()
        st.session_state[DASHBOARD_LITE_OPTION_CACHE_KEY] = option_cache
    stock_codes = _clean_list(option_cache.get("stock_codes"))
    stock_code_to_name = dict(option_cache.get("stock_code_to_name") or {})
    product_group_codes = _clean_list(option_cache.get("product_group_codes"))
    product_group_code_to_name = dict(option_cache.get("product_group_code_to_name") or {})
    product_di_codes = _clean_list(option_cache.get("product_di_codes"))
    product_di_code_to_name = dict(option_cache.get("product_di_code_to_name") or {})
    product_class_codes = _clean_list(option_cache.get("product_class_codes"))
    product_class_code_to_name = dict(option_cache.get("product_class_code_to_name") or {})
    vendor_group_codes = _clean_list(option_cache.get("vendor_group_codes"))
    vendor_group_code_to_name = dict(option_cache.get("vendor_group_code_to_name") or {})
    vendor_kind_codes = _clean_list(option_cache.get("vendor_kind_codes"))
    vendor_kind_code_to_name = dict(option_cache.get("vendor_kind_code_to_name") or {})
    io_gu_codes = _clean_list(option_cache.get("io_gu_codes"))
    io_gu_code_to_name = dict(option_cache.get("io_gu_code_to_name") or {})
    stock_widget_key = "__dashboard_lite_stock_labels"
    _prepare_dashboard_multiselect_state(stock_widget_key, stock_codes)
    _prepare_dashboard_multiselect_state("__dashboard_lite_vendor_group_list", vendor_group_codes)
    _prepare_dashboard_multiselect_state("__dashboard_lite_vendor_kind_list", vendor_kind_codes)
    _prepare_dashboard_multiselect_state("__dashboard_lite_product_group_list", product_group_codes)
    _prepare_dashboard_multiselect_state("__dashboard_lite_product_di_list", product_di_codes)
    _prepare_dashboard_multiselect_state("__dashboard_lite_product_class_list", product_class_codes)
    _prepare_dashboard_multiselect_state("__dashboard_lite_io_gu_list", io_gu_codes)
    selected_stock_count = len(_clean_list(st.session_state.get("__dashboard_lite_stock_labels")))
    stock_scope = "\uc804\uccb4" if selected_stock_count == 0 else f"{selected_stock_count}\uac1c"
    stock_basis = "\uc2e4\uc7ac\uace0" if st.session_state.get("__dashboard_lite_stock_mode", "real") == "real" else "\uc7a5\ubd80\uc7ac\uace0"
    risk_days = st.session_state.get("__dashboard_lite_risk_analysis_days", 90)
    readiness = st.session_state.get("__dashboard_lite_readiness_warning_pct", 98)
    io_count = len(_clean_list(st.session_state.get("__dashboard_lite_io_gu_list")))
    io_summary = "\uc785\ucd9c\uace0 \uc804\uccb4" if io_count == 0 else f"\uc785\ucd9c\uace0 {io_count}\uac1c"
    condition_summary = f"{stock_basis} \u00b7 \uc7ac\uace0\uc704\uce58 {stock_scope} \u00b7 {io_summary} \u00b7 \uc704\ud5d8 {risk_days}\uc77c \u00b7 \uc900\ube44\uc728 {readiness}%"
    for key, default_value in (
        ("__dashboard_lite_month_from", defaults["month_from"]),
        ("__dashboard_lite_month_to", defaults["month_to"]),
        ("__dashboard_lite_evaluation_month", defaults["evaluation_month"]),
    ):
        if key not in st.session_state:
            st.session_state[key] = default_value
    if st.session_state.get("__dashboard_lite_product_supplier_scope_mode") not in {SCOPE_MANUFACTURER, SCOPE_ORDER_VENDOR}:
        st.session_state["__dashboard_lite_product_supplier_scope_mode"] = SCOPE_MANUFACTURER

    scope_cols = st.columns([1, 1, 1, 1.1, 2.1, 2.1], gap="small")
    with scope_cols[0]:
        month_from = st.text_input("시작월", max_chars=6, help="YYYYMM", key="__dashboard_lite_month_from")
    with scope_cols[1]:
        month_to = st.text_input("종료월", max_chars=6, help="YYYYMM", key="__dashboard_lite_month_to")
    with scope_cols[2]:
        evaluation_month = st.text_input("평가월", max_chars=6, help="YYYYMM", key="__dashboard_lite_evaluation_month")
    with scope_cols[3]:
        scope_mode = st.selectbox(
            "공급 기준", options=[SCOPE_MANUFACTURER, SCOPE_ORDER_VENDOR],
            format_func=lambda value: {SCOPE_MANUFACTURER: "제약사", SCOPE_ORDER_VENDOR: "발주처"}[value],
            key="__dashboard_lite_product_supplier_scope_mode",
            on_change=_on_dashboard_supplier_scope_mode_change,
        )
    inactive_scope_cleared = _clear_inactive_dashboard_supplier_state(scope_mode)
    with scope_cols[4]:
        if scope_mode == SCOPE_ORDER_VENDOR:
            supplier_text = st.text_input("발주처", key="__dashboard_lite_order_vendor_text")
        else:
            supplier_text = st.text_input("제약사", key="__dashboard_lite_manufacturer_text")
    manager_options: list[dict[str, str]] = []
    manager_option_error = False
    try:
        manager_options = _dashboard_supplier_manager_options(scope_mode)
    except Exception:
        manager_option_error = True
    manager_codes = [row["code"] for row in manager_options]
    manager_names = {row["code"]: row["name"] for row in manager_options}
    manager_key = "__dashboard_lite_manufacturer_manager_codes" if scope_mode == SCOPE_MANUFACTURER else "__dashboard_lite_purchase_manager_codes"
    with scope_cols[5]:
        st.session_state[manager_key] = [code for code in _clean_list(st.session_state.get(manager_key)) if code in manager_codes]
        if manager_option_error:
            st.warning("담당자 목록을 불러오지 못했습니다.")
        else:
            st.multiselect(
                "제약사 담당자" if scope_mode == SCOPE_MANUFACTURER else "발주담당자",
                options=manager_codes, key=manager_key,
                format_func=lambda code: f"{manager_names.get(str(code), str(code))} [{code}]",
            )
    with st.form("dashboard_lite_scope_form", clear_on_submit=False):
        with st.expander(f"\ucd94\uac00 \ubd84\uc11d\uc870\uac74 \u00b7 {condition_summary}", expanded=False):
            row1 = st.columns([1, 3, 1])
            with row1[0]:
                stock_mode = st.radio("재고기준", options=["real", "book"], horizontal=True, format_func=lambda value: "실재고" if value == "real" else "장부재고", key="__dashboard_lite_stock_mode")
            with row1[1]:
                selected_stock_labels = st.multiselect("재고위치", options=stock_codes, key=stock_widget_key, format_func=lambda code: _option_label(code, stock_code_to_name.get(str(code), "")), help="미선택 시 전체 재고위치를 사용합니다.")
            with row1[2]:
                amount_display_unit = st.selectbox("금액 표시단위", options=["auto", "won", "thousand", "million"], format_func=lambda value: {"auto": "자동", "won": "원", "thousand": "천원", "million": "백만원"}[value], key="__dashboard_lite_amount_display_unit")

            row2 = st.columns(2)
            with row2[0]:
                vendor_groups = st.multiselect("거래처그룹", options=vendor_group_codes, key="__dashboard_lite_vendor_group_list", format_func=lambda code: _option_label(code, vendor_group_code_to_name.get(str(code), "")))
            with row2[1]:
                vendor_kinds = st.multiselect("거래처종류", options=vendor_kind_codes, key="__dashboard_lite_vendor_kind_list", format_func=lambda code: _option_label(code, vendor_kind_code_to_name.get(str(code), "")))

            row3 = st.columns(4)
            with row3[0]:
                product_groups = st.multiselect("제품그룹", options=product_group_codes, key="__dashboard_lite_product_group_list", format_func=lambda code: _option_label(code, product_group_code_to_name.get(str(code), "")), help="미선택 시 전체 제품그룹을 포함합니다.")
            with row3[1]:
                product_di = st.multiselect("제품구분", options=product_di_codes, key="__dashboard_lite_product_di_list", format_func=lambda code: _option_label(code, product_di_code_to_name.get(str(code), "")), help="미선택 시 전체 제품구분을 포함합니다.")
            with row3[2]:
                product_class = st.multiselect("제품분류", options=product_class_codes, key="__dashboard_lite_product_class_list", format_func=lambda code: _option_label(code, product_class_code_to_name.get(str(code), "")), help="미선택 시 전체 제품분류를 포함합니다.")
            with row3[3]:
                io_gu = st.multiselect("입출고구분", options=io_gu_codes, key="__dashboard_lite_io_gu_list", format_func=lambda code: _option_label(code, io_gu_code_to_name.get(str(code), "")), help="저장한 Dashboard 공통조건이 Dashboard, KPI, 분석/NLQ의 판매·수요 계산에 적용됩니다.")

            row4 = st.columns(5)
            with row4[0]:
                major_purchase_vendor_days = st.number_input("대표 매입처 기준기간(일)", min_value=1, step=1, key="__dashboard_lite_major_purchase_vendor_days")
            with row4[1]:
                risk_analysis_days = st.number_input("위험 분석기간(일)", min_value=1, step=1, key="__dashboard_lite_risk_analysis_days")
            with row4[2]:
                overstock_inactive_days = st.number_input("과잉·저활성 기준(일)", min_value=1, step=1, key="__dashboard_lite_overstock_inactive_days")
            with row4[3]:
                readiness_warning_pct = st.number_input("준비율 경고기준(%)", min_value=0.1, max_value=100.0, step=0.1, key="__dashboard_lite_readiness_warning_pct")
            with row4[4]:
                risk_quick_view_count = st.number_input("위험품목 바로보기", min_value=1, step=1, key="__dashboard_lite_risk_quick_view_count")
        submitted = st.form_submit_button("대시보드 조회", type="primary", width="stretch")
        try:
            from app.ui.ssai_login import has_permission
            save_requested = st.form_submit_button("저장", width="stretch") if has_permission(PROFILE_PERMISSION) else False
        except Exception:
            save_requested = False
    supplier_result = {"status": "all", "codes": [], "names": [], "label": "전체"}
    if submitted:
        supplier_result = _resolve_dashboard_supplier(supplier_text, mode=scope_mode)
        if supplier_result.get("status") == "too_short":
            st.warning("거래처명 검색은 두 글자 이상 입력해 주세요.")
            return False, False, None
        if supplier_result.get("status") in {"missing", "error"}:
            st.warning("해당 거래처를 찾을 수 없습니다.")
            return False, False, None
    stock_cd_list = _clean_list(selected_stock_labels)
    stock_name_list = [stock_code_to_name.get(code, "") for code in stock_cd_list]
    product_group_codes_selected = _clean_list(product_groups)
    product_di_codes_selected = _clean_list(product_di)
    product_class_codes_selected = _clean_list(product_class)
    selected_manager_codes = _clean_list(st.session_state.get(manager_key))
    selected_manager_labels = [f"{manager_names.get(code, code)} [{code}]" for code in selected_manager_codes]
    supplier_manager_label = "전체" if not selected_manager_labels else (selected_manager_labels[0] if len(selected_manager_labels) == 1 else f"{len(selected_manager_labels)}명")
    raw_params = {
        "month_from": month_from,
        "month_to": month_to,
        "evaluation_month": evaluation_month,
        "source_mode": defaults["source_mode"],
        "stock_mode": stock_mode,
        "stock_cd_list": stock_cd_list,
        "stock_name_list": stock_name_list,
        "vendor_group_list": _clean_list(vendor_groups),
        "vendor_kind_list": _clean_list(vendor_kinds),
        "product_group_list": product_group_codes_selected,
        "product_di_list": product_di_codes_selected,
        "product_class_list": product_class_codes_selected,
        "io_gu_list": (
            [str(value).split(":", 1)[-1] for value in _clean_list(io_gu)]
            if save_requested
            else _clean_list(st.session_state.get("__dashboard_lite_company_io_gu_list"))
        ),
        "io_gu_source": "company_default",
        "_require_company_io": True,
        "product_supplier_scope_mode": scope_mode,
        "manufacturer_codes": supplier_result["codes"] if scope_mode == SCOPE_MANUFACTURER else [],
        "manufacturer_manager_codes": selected_manager_codes if scope_mode == SCOPE_MANUFACTURER else [],
        "order_vendor_codes": supplier_result["codes"] if scope_mode == SCOPE_ORDER_VENDOR else [],
        "purchase_manager_codes": selected_manager_codes if scope_mode == SCOPE_ORDER_VENDOR else [],
        "supplier_scope_label": supplier_result["label"],
        "supplier_scope_names": supplier_result["names"],
        "supplier_manager_label": supplier_manager_label,
        "supplier_manager_labels": selected_manager_labels,
        "inactive_scope_cleared": inactive_scope_cleared,
        "major_purchase_vendor_days": major_purchase_vendor_days,
        "risk_analysis_days": risk_analysis_days,
        "overstock_inactive_days": overstock_inactive_days,
        "readiness_warning_pct": readiness_warning_pct,
        "risk_quick_view_count": risk_quick_view_count,
        "amount_display_unit": amount_display_unit,
    }
    try:
        params = normalize_dashboard_lite_params(raw_params, today=date.today())
        log.info("[dashboard.supplier_scope] mode=%s vendor_count=%s manager_count=%s inactive_scope_cleared=%s", params.get("product_supplier_scope_mode"), len(params.get("manufacturer_codes") or params.get("order_vendor_codes") or []), len(params.get("manufacturer_manager_codes") or params.get("purchase_manager_codes") or []), bool(params.get("inactive_scope_cleared")))
    except Exception as exc:
        st.warning(str(exc))
        return False, False, None
    return submitted, save_requested, params


def _dashboard_scope_header(params: dict[str, Any]) -> str:
    """Return the concise, user-facing scope line for the dedicated result block."""
    parts = [
        f"조회기간: {params.get('month_from') or '-'}~{params.get('month_to') or '-'}",
        f"평가월: {params.get('evaluation_month') or '-'}",
    ]
    scope = normalize_product_supplier_scope(params)
    mode = scope["product_supplier_scope_mode"]
    mode_labels = {SCOPE_MANUFACTURER: "제약사", SCOPE_ORDER_VENDOR: "발주처"}
    parts.append(f"공급 기준: {mode_labels[mode]}")
    if mode == SCOPE_MANUFACTURER:
        manufacturer_label = str(params.get("supplier_scope_label") or params.get("manufacturer_scope_label") or "전체").strip() or "전체"
        parts.append(f"제약사: {manufacturer_label}")
        parts.append(f"제약사 담당자: {str(params.get('supplier_manager_label') or '전체').strip() or '전체'}")
    elif mode == SCOPE_ORDER_VENDOR:
        parts.append(f"발주처: {str(params.get('supplier_scope_label') or '전체').strip() or '전체'}")
        parts.append(f"발주담당자: {str(params.get('supplier_manager_label') or '전체').strip() or '전체'}")
    stock_codes = _clean_list(params.get("stock_cd_list"))
    stock_names = _clean_list(params.get("stock_name_list"))
    stock_labels = [
        f"{stock_names[index]} [{code}]" if index < len(stock_names) and stock_names[index] else code
        for index, code in enumerate(stock_codes)
    ]
    if stock_labels:
        parts.append(f"재고위치: {', '.join(stock_labels)}")

    for label, name_key, code_key in (
        ("제품그룹", "product_group_nm_list", "product_group_list"),
        ("제품구분", "product_di_nm_list", "product_di_list"),
        ("제품분류", "product_class_nm_list", "product_class_list"),
    ):
        selected = _clean_list(params.get(name_key)) or _clean_list(params.get(code_key))
        if selected:
            parts.append(f"{label}: {', '.join(selected)}")
    return " · ".join(parts)


def build_dashboard_lite_chat_snapshot(cache: Any) -> dict[str, Any]:
    """Keep the immutable chat rendering contract without persisting full readiness rows."""
    source = dict(cache or {}) if isinstance(cache, dict) else {}
    params = dict(source.get("params") or {})
    facts = dict(source.get("facts") or {})
    sales = dict(facts.get("sales") or {})
    inventory = dict(facts.get("inventory") or {})
    try:
        requested_limit = int(params.get("risk_quick_view_count") or 10)
    except (TypeError, ValueError):
        requested_limit = 10
    row_limit = max(10, min(requested_limit, 30))
    compact_params = {
        key: params.get(key)
        for key in (
            "month_from", "month_to", "evaluation_month", "stock_mode", "stock_cd_list", "stock_name_list",
            "product_group_list", "product_di_list", "product_class_list", "amount_display_unit",
            "io_gu_list", "io_gu_source",
            "product_supplier_scope_mode", "supplier_scope_label", "supplier_manager_label", "supplier_manager_labels",
        )
        if key in params
    }
    compact_params["amount_display_unit_requested"] = params.get("amount_display_unit_requested") or params.get("amount_display_unit")
    compact_params["amount_display_unit_resolved"] = (
        params.get("amount_display_unit_resolved")
        or (facts.get("filters") or {}).get("amount_display_unit_resolved")
        or (facts.get("filters") or {}).get("amount_display_unit")
    )
    compact_params["product_supplier_scope_mode"] = normalize_product_supplier_scope(params)["product_supplier_scope_mode"]
    compact_params["manufacturer_codes"] = _clean_list(params.get("manufacturer_codes"))[:row_limit]
    compact_params["manufacturer_manager_codes"] = _clean_list(params.get("manufacturer_manager_codes"))[:row_limit]
    compact_params["order_vendor_codes"] = _clean_list(params.get("order_vendor_codes"))[:row_limit]
    compact_params["purchase_manager_codes"] = _clean_list(params.get("purchase_manager_codes"))[:row_limit]
    compact_facts = {
        "kind": facts.get("kind"),
        "period": facts.get("period"),
        "filters": {
            "amount_display_unit": _facts_amount_display_unit(facts),
            "amount_display_unit_requested": (facts.get("filters") or {}).get("amount_display_unit_requested"),
            "amount_display_unit_resolved": (facts.get("filters") or {}).get("amount_display_unit_resolved"),
        },
        "sales": {
            "metrics": dict(sales.get("metrics") or {}),
            "visualization": dict(sales.get("visualization") or {}),
            "chart_rows": list(sales.get("chart_rows") or []),
            "decline_targets": list(sales.get("decline_targets") or [])[:row_limit],
        },
        "inventory": {
            "metrics": dict(inventory.get("metrics") or {}),
            "risk_targets": list(inventory.get("risk_targets") or [])[:row_limit],
            "stock_risk_summary": list(inventory.get("stock_risk_summary") or []),
            "stock_overstock_summary": dict(inventory.get("stock_overstock_summary") or {}),
            "stock_demand_surge_summary": dict(inventory.get("stock_demand_surge_summary") or {}),
            "vendor_stock_risk_summary": dict(inventory.get("vendor_stock_risk_summary") or {}),
            "vendor_stock_risk_top_rows": list(inventory.get("vendor_stock_risk_top_rows") or [])[:row_limit],
            "inbound_summary": {
                key: (inventory.get("inbound_summary") or {}).get(key)
                for key in (
                    "cycle_days", "vendor_days", "delayed_products", "insufficient_products",
                    "fallback_products", "inbound_source_call_count", "inbound_io_policy",
                )
            },
            "inbound_metadata": {
                "inbound_cycle_days": (inventory.get("inbound_summary") or {}).get("cycle_days"),
                "inbound_vendor_days": (inventory.get("inbound_summary") or {}).get("vendor_days"),
                "inbound_delayed_count": (inventory.get("inbound_summary") or {}).get("delayed_products"),
                "inbound_insufficient_count": (inventory.get("inbound_summary") or {}).get("insufficient_products"),
                "inbound_fallback_count": (inventory.get("inbound_summary") or {}).get("fallback_products"),
                "inbound_source_call_count": (inventory.get("inbound_summary") or {}).get("inbound_source_call_count"),
            },
            "risk_detail_summary": {
                key: (inventory.get("risk_detail_summary") or {}).get(key)
                for key in ("source_rows", "emergency_rows", "warning_rows", "amount_positive_rows", "zero_amount_rows")
            },
            "data_quality": list(inventory.get("data_quality") or [])[:row_limit],
        },
        "turnover_days": dict(facts.get("turnover_days") or {}),
        "today_actions": list(facts.get("today_actions") or [])[:row_limit],
        "data_quality": list(facts.get("data_quality") or [])[:row_limit],
        "performance": dict(facts.get("performance") or {}),
    }
    return {
        "snapshot_version": 1,
        "cache_key": source.get("cache_key"),
        "query_fingerprint": source.get("query_fingerprint"),
        "company_id": source.get("company_id"),
        "room_id": source.get("room_id"),
        "dashboard_event_id": source.get("dashboard_event_id"),
        "params": compact_params,
        "facts": compact_facts,
        "elapsed_ms": source.get("elapsed_ms"),
        "elapsed_seconds": source.get("elapsed_seconds"),
        "created_at": source.get("created_at"),
    }


def _mark_dashboard_room_title() -> bool:
    """Name only a still-empty auto-created room; never replace a normal query title."""
    ss = st.session_state
    room_id = str(
        ss.get("__chat_current_room_id")
        or get_current_chat_room_id()
        or ss.get("current_room")
        or ""
    ).strip()
    if not room_id:
        return False
    rooms = ss.get("chat_rooms") or []
    for room in rooms:
        if not isinstance(room, dict) or str(room.get("id") or "") != room_id:
            continue
        has_messages = any(bool(room.get(key)) for key in ("messages", "history", "sims_messages", "gen_messages"))
        if (
            room.get("auto_created") is not True
            or room.get("name_auto") is not True
            or room.get("title_initialized") is True
            or has_messages
        ):
            return False
        created_at = str(room.get("created_at") or "").strip()
        created_title_time = created_at.replace("T", " ")[:16]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", created_title_time):
            title_time = created_title_time
            title_source = "room_created_at"
        else:
            title_time = datetime.now().strftime("%Y-%m-%d %H:%M")
            title_source = "current_time"
        room_title = f"{title_time} Dashboard Lite"
        # Preserve this fact before the title mutation.  The panel uses it to
        # create the persisted-room navigation request before it pushes the
        # first Dashboard message.
        ss["__dashboard_lite_pending_room_id"] = room_id
        room["name"] = room_title
        room["auto_created"] = False
        room["name_auto"] = False
        room["title_initialized"] = True
        room["title_source"] = "dashboard_lite"
        room["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ss["__chat_room_title_changed_room_id"] = room_id
        ss["__chat_room_title_changed_name"] = room_title
        log.info(
            "[dashboard.room_title] room_title_set=True title_has_timestamp=True title_source=%s",
            title_source,
        )
        return True
    return False


def _render_dashboard_result_header(cache: dict[str, Any]) -> None:
    params = dict(cache.get("params") or {})
    elapsed_ms = int(cache.get("elapsed_ms") or 0)
    created_at = str(cache.get("created_at") or "").strip()
    st.markdown("## 일일 재고·매출 보고")
    st.caption(_dashboard_scope_header(params))
    if created_at:
        st.caption(f"조회 완료 · {max(0, elapsed_ms) / 1000:.1f}초 · {created_at}")
    else:
        st.caption(f"조회 완료 · {max(0, elapsed_ms) / 1000:.1f}초")


def _render_dashboard_facts(
    facts: dict[str, Any],
    cache: dict[str, Any] | None = None,
    *,
    render_mode: str,
) -> None:
    filter_issues = [
        item
        for item in (facts.get("data_quality") or [])
        if isinstance(item, dict) and item.get("filter_basis") == "not_applied"
    ]
    if filter_issues:
        labels = ", ".join(str(item.get("label") or "제품 조건") for item in filter_issues)
        st.warning(f"{labels} 제외 조건에 필요한 코드 컬럼이 없어 이번 결과에는 적용하지 않았습니다.")
    sales_gauge_col, sales_chart_col = st.columns([35, 65])
    with sales_gauge_col:
        _render_sales_gauge(facts)
    with sales_chart_col:
        st.markdown("### 월별 실제매출·목표")
        st.caption("파란 막대는 실제매출, 주황 목표선은 월 예상, 청록 목표선은 현재일 기준 예상입니다.")
        _render_sales_chart(facts)
    _render_sales_brief(facts)
    st.divider()

    stock_summary_col, stock_chart_col = st.columns([35, 65])
    with stock_summary_col:
        _render_stock_risk_summary(facts)
    with stock_chart_col:
        st.markdown("### 재고 준비율 미달 TOP 10")
        readiness_threshold = float((facts.get("stock_readiness") or {}).get("threshold_pct") or 98.0)
        st.caption(f"준비율 경고기준 {_fmt_threshold_pct(readiness_threshold)}% 미만 품목 중 위험도가 높은 품목을 표시합니다.")
        _render_stock_chart(facts)

    _render_vendor_stock_risk(facts)
    _render_demand_surge_detail_summary(facts)

    st.markdown("### 매입·매출 거래 회전일")
    _render_turnover(facts)

    st.markdown("### 오늘의 조치")
    _render_today_actions(facts, cache or {}, render_mode=render_mode)
    _render_selected_dashboard_action_detail(facts, cache or {}, render_mode=render_mode)
    _render_risk_detail(facts, cache or {}, render_mode=render_mode)

def _render_dashboard_result_in_primary_area(cache: dict[str, Any]) -> None:
    """Render the active result inline at its chat-history message position."""
    _render_dashboard_result_header(cache)
    _render_dashboard_facts(dict(cache.get("facts") or {}), cache, render_mode="primary")


def reset_dashboard_lite_primary_render_guard() -> None:
    """Reset the one-shot primary renderer at the beginning of a script rerun."""
    st.session_state["__dashboard_lite_primary_rendered_this_run"] = False


def _try_render_dashboard_primary_once(cache: dict[str, Any], *, caller: str, reason: str) -> bool:
    """Render one interactive Dashboard at most once in the current rerun."""
    if bool(st.session_state.get("__dashboard_lite_primary_rendered_this_run")):
        log.info(
            "[dashboard.primary_render] action=skip_already_rendered caller=%s reason=%s guard_before=True guard_after=True cache_available=True target_available=%s detail_rows_available=%s",
            caller,
            reason,
            _DASHBOARD_RENDER_TARGET is not None,
            len((((cache.get("facts") or {}).get("inventory") or {}).get("risk_detail_rows") or [])),
        )
        return False

    st.session_state["__dashboard_lite_primary_rendered_this_run"] = True
    log.info(
        "[dashboard.primary_render] action=render caller=%s reason=%s guard_before=False guard_after=True cache_available=True target_available=%s detail_rows_available=%s",
        caller,
        reason,
        _DASHBOARD_RENDER_TARGET is not None,
        len((((cache.get("facts") or {}).get("inventory") or {}).get("risk_detail_rows") or [])),
    )
    _render_dashboard_result_in_primary_area(cache)
    return True


def render_cached_dashboard_lite_primary(*, caller: str = "main_top", reason: str = "cached_rerun") -> bool:
    """Re-render cached facts in the main result area without any source reload."""
    cache = st.session_state.get("__dashboard_lite_result")
    identity = _dashboard_context_identity()
    ownership = dashboard_lite_primary_cache_matches_context(
        cache,
        current_room_id=get_current_chat_room_id(),
        current_company_id=identity.get("company_id"),
    )
    if ownership["cache_available"] and not (ownership["room_match"] and ownership["company_match"]):
        clear_dashboard_lite_active_result(st.session_state)
        log.info(
            "[dashboard.primary_render] action=skip_room_mismatch caller=%s cache_available=True room_match=%s company_match=%s cache_cleared=True",
            caller,
            ownership["room_match"],
            ownership["company_match"],
        )
        return False
    facts = cache.get("facts") if isinstance(cache, dict) else None
    if not isinstance(facts, dict) or not facts:
        log.info(
            "[dashboard.primary_render] action=no_cache caller=%s reason=%s guard_before=%s guard_after=%s cache_available=False target_available=%s detail_rows_available=0",
            caller,
            reason,
            bool(st.session_state.get("__dashboard_lite_primary_rendered_this_run")),
            bool(st.session_state.get("__dashboard_lite_primary_rendered_this_run")),
            _DASHBOARD_RENDER_TARGET is not None,
        )
        return False
    return _try_render_dashboard_primary_once(cache, caller=caller, reason=reason)


def render_dashboard_lite_chat_item(cache: dict[str, Any], *, render_mode: str = "chat") -> bool:
    """Render a Dashboard once at its chronological chat-message position."""
    if render_mode == "primary":
        return _try_render_dashboard_primary_once(
            cache,
            caller="chat_history",
            reason="active_message",
        )
    _render_dashboard_result_header(cache)
    _render_dashboard_facts(dict(cache.get("facts") or {}), cache, render_mode="chat")
    return True


def render_dashboard_lite() -> dict[str, Any]:
    """Render Dashboard Lite without changing current-table routing."""
    st.subheader("일일 재고·매출 보고")
    st.caption("상태 → 근거 → 무엇을 해야 하나 순서로 읽는 운영 브리핑입니다.")

    _apply_saved_dashboard_profile_once()
    submitted, save_requested, params = _render_dashboard_scope_form()
    run_seq = int(st.session_state.get("__dashboard_lite_run_seq") or 0)
    cache = st.session_state.get("__dashboard_lite_result")

    if submitted:
        if not _clean_list(params.get("io_gu_list")):
            st.warning("회사 공통 분석용 입출고구분이 설정되지 않았습니다. Dashboard 공통조건에서 설정 후 저장해 주세요.")
            submitted = False
        else:
            st.session_state["__dashboard_lite_run_seq"] = run_seq + 1
            run_seq += 1
            for key in list(st.session_state.keys()):
                if str(key).startswith("__dashboard_lite_risk_detail_excel::"):
                    st.session_state.pop(key, None)
            st.session_state.pop("__dashboard_lite_result", None)
            cache = None

    if save_requested and params:
        identity = _dashboard_context_identity()
        try:
            from app.ui.ssai_login import has_permission
            allowed = bool(has_permission(PROFILE_PERMISSION))
        except Exception:
            allowed = False
        if not allowed or not identity.get("user_id") or not identity.get("company_id"):
            st.error("저장 권한이 없습니다.")
        else:
            try:
                action = save_dashboard_profile(company_id=int(identity["company_id"]), params=params, actor_user_id=int(identity["user_id"]))
                mark_analysis_profile_saved(st.session_state, company_id=identity["company_id"])
                st.session_state["__dashboard_lite_company_io_gu_list"] = _clean_list(params.get("io_gu_list"))
                st.success("조회조건을 저장했습니다." if action else "조회조건을 저장했습니다.")
            except Exception as exc:
                log.warning("[dashboard.profile_save] user_id=%s company_id=%s saved=False error_type=%s", identity.get("user_id"), identity.get("company_id"), type(exc).__name__)
                st.error("조회조건을 저장할 수 없습니다. 관리자에게 migration 적용 여부를 확인해 주세요.")

    cache_key = _dashboard_cache_key(params, run_seq=run_seq) if params else ""
    # Editing the form must not hide the last completed Dashboard. It is replaced
    # only by another submit, company change, logout, or the explicit option reset.
    if cache:
        facts = cache.get("facts") or {}
        return {
            "final": False,
            "type": "text",
            "title": "Dashboard Lite v0.1",
            "action": "Dashboard Lite v0.1",
            "data": "Dashboard Lite v0.1 화면을 표시했습니다.",
            "meta": {"analysis_type": "dashboard_lite", "facts_kind": facts.get("kind")},
        }

    if not submitted:
        st.info("조회 조건을 확인한 뒤 [대시보드 조회]를 눌러 주세요.")
        return {
            "final": False,
            "type": "text",
            "title": "Dashboard Lite v0.1",
            "action": "Dashboard Lite v0.1",
            "data": "Dashboard Lite 조건 화면을 표시했습니다.",
            "meta": {"analysis_type": "dashboard_lite", "status": "condition_only"},
        }

    if not params:
        return {
            "final": False,
            "type": "text",
            "title": "Dashboard Lite v0.1",
            "action": "Dashboard Lite v0.1",
            "data": "Dashboard Lite 조회 범위가 올바르지 않습니다.",
            "meta": {"analysis_type": "dashboard_lite", "status": "invalid_scope"},
        }

    identity = _dashboard_context_identity()
    params = dict(params)
    if identity.get("company_id"):
        params["company_id"] = identity["company_id"]

    log.info(
        "[dashboard.start] company_id=%s month_from=%s month_to=%s evaluation_month=%s source_call_count=%s elapsed_ms=%s",
        params.get("company_id") or "",
        params.get("month_from"),
        params.get("month_to"),
        params.get("evaluation_month"),
        0,
        0,
    )
    started = time.perf_counter()
    try:
        with st.spinner("Dashboard 조회 중"):
            facts = build_dashboard_lite_facts(params)
    except Exception as exc:
        st.error("Dashboard Lite facts를 생성하지 못했습니다. 기존 상세 조회 화면을 사용해 주세요.")
        return {
            "final": False,
            "type": "text",
            "title": "Dashboard Lite v0.1",
            "action": "Dashboard Lite v0.1",
            "data": "Dashboard Lite facts 생성 실패",
            "meta": {"analysis_type": "dashboard_lite", "error_type": type(exc).__name__},
        }

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    requested_amount_unit = str(params.get("amount_display_unit") or "auto").strip().lower()
    resolved_amount_unit = _resolved_dashboard_amount_unit(facts, requested_amount_unit)
    params["amount_display_unit_requested"] = requested_amount_unit
    params["amount_display_unit_resolved"] = resolved_amount_unit
    facts_filters = dict(facts.get("filters") or {})
    facts_filters["amount_display_unit_requested"] = requested_amount_unit
    facts_filters["amount_display_unit_resolved"] = resolved_amount_unit
    # Renderers read this event-local value, never the current form widget.
    facts_filters["amount_display_unit"] = resolved_amount_unit
    facts["filters"] = facts_filters
    result_cache = {
        "cache_key": cache_key,
        "query_fingerprint": _dashboard_cache_key(params, run_seq=0),
        "company_id": identity.get("company_id") or "",
        "room_id": get_current_chat_room_id(),
        "params": params,
        "facts": facts,
        "elapsed_ms": elapsed_ms,
        "elapsed_seconds": round(elapsed_ms / 1000.0, 3),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    event_id = str(uuid.uuid4())
    result_cache["dashboard_event_id"] = event_id
    for action in facts.get("today_actions") or []:
        if isinstance(action, dict):
            action["source_dashboard_event_id"] = event_id
    st.session_state["__dashboard_lite_result"] = result_cache
    _mark_dashboard_room_title()
    return {
        "id": event_id,
        "final": True,
        "type": "dashboard_lite",
        "title": "Dashboard Lite",
        "action": "Dashboard Lite v0.1",
        "params": dict(params),
        "data": None,
        "meta": {
            "analysis_type": "dashboard_lite",
            "facts_kind": facts.get("kind"),
            "room_id": result_cache["room_id"],
            "dashboard_event_id": event_id,
            "dashboard_cache": build_dashboard_lite_chat_snapshot(result_cache),
            "query_summary": _dashboard_scope_header(params),
        },
    }

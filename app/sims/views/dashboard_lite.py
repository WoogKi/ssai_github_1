# app/sims/views/dashboard_lite.py
# -*- coding: utf-8 -*-
"""Dashboard Lite v0.1 Streamlit view."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import html
import json
import logging
import math
import re
import time
import uuid
from typing import Any, Mapping

import altair as alt
import pandas as pd
import streamlit as st

from app.services.dashboard_lite_facts import (
    build_dashboard_lite_facts,
    default_dashboard_lite_settings,
    default_dashboard_lite_scope,
    filter_dashboard_risk_detail_rows,
    normalize_dashboard_lite_params,
    resolve_transaction_cycle_status,
)
from app.services.dashboard_risk_detail_export import (
    build_dashboard_inventory_detail_excel_bytes,
    build_dashboard_risk_detail_excel_bytes,
)
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
from app.ui.sims_table_display import (
    build_sims_table_display_config,
    is_sims_numeric_display_col,
    normalize_display_df_for_streamlit,
)


log = logging.getLogger("ssai.sims.dashboard_lite")

DASHBOARD_LITE_SESSION_KEYS = (
    "__dashboard_lite_result",
    "__dashboard_lite_applied_params",
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
    "__dashboard_layout_v2_preview",
)

DASHBOARD_LITE_OPTION_CACHE_KEY = "__dashboard_lite_scope_options"
DASHBOARD_LITE_OPTION_CACHE_VERSION = 3
_DASHBOARD_RENDER_TARGET: Any | None = None
_DASHBOARD_V2_PREVIEW_AVAILABLE = False

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

_DASHBOARD_PROFILE_SCALAR_DEFAULTS = default_dashboard_lite_settings()


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
        if text in {"__dashboard_lite_result", "__dashboard_lite_applied_params", "__dashboard_selected_action_detail", "__dashboard_lite_suppress_chat_autoscroll_once"} or text.startswith("__dashboard_lite_risk_detail_"):
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


def _dashboard_readiness_threshold(
    facts: dict[str, Any] | None,
    params: dict[str, Any] | None = None,
) -> float:
    """Resolve facts, request, and central-default threshold in that order."""
    raw = ((facts or {}).get("stock_readiness") or {}).get("threshold_pct")
    if raw is None:
        raw = (params or {}).get("readiness_warning_pct")
    if raw is None:
        raw = _DASHBOARD_PROFILE_SCALAR_DEFAULTS["readiness_warning_pct"]
    return float(raw)


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


_DASHBOARD_INLINE_ICON_PATHS = {
    "box": '<path d="M4 8.5 12 4l8 4.5v8L12 21l-8-4.5z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M4 8.5 12 13l8-4.5M12 13v8M8 6.2l8 4.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
    "bars": '<path d="M5 19V13h3v6zm5 0V9h3v10zm5 0V5h3v14z" fill="currentColor"/>',
    "cycle": '<path d="M7 7h8l-2.5-2.5M17 17H9l2.5 2.5M18.5 10A7 7 0 0 0 7 7M5.5 14A7 7 0 0 0 17 17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
    "trend": '<path d="M4 17l5-5 4 3 7-8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M15 7h5v5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    "truck": '<path d="M3 6h11v10H3zM14 10h3l3 3v3h-6z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><circle cx="7" cy="18" r="2" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="17" cy="18" r="2" fill="none" stroke="currentColor" stroke-width="1.8"/>',
    "calendar": '<rect x="4" y="5" width="16" height="15" rx="2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M8 3v4m8-4v4M4 9h16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><rect x="8" y="12" width="3" height="3" rx=".5" fill="currentColor"/>',
    "pie": '<path d="M12 3a9 9 0 1 0 9 9h-9z" fill="currentColor" opacity=".92"/><path d="M14 3.2V10h6.8A9 9 0 0 0 14 3.2z" fill="currentColor" opacity=".62"/>',
    "report": '<path d="M6 3h9l3 3v15H6z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M9 11h6M9 15h3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="17.5" cy="17.5" r="3.2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="m16.2 17.5.8.8 1.8-2" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>',
    "compare": '<path d="M5 12a7 7 0 0 1 12-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M17 4v5h-5M19 12a7 7 0 0 1-12 4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M7 20v-5h5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    "target": '<circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="2"/><path d="m12 12 7-7m-3 0h3v3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
}


def _dashboard_inline_icon(
    icon_name: str,
    semantic_class: str,
    *,
    variant: str = "circle",
) -> str:
    path = _DASHBOARD_INLINE_ICON_PATHS.get(str(icon_name or "").strip())
    if not path:
        return ""
    safe_semantic = re.sub(r"[^a-z0-9_-]", "", str(semantic_class or "neutral").lower()) or "neutral"
    safe_variant = re.sub(r"[^a-z0-9_-]", "", str(variant or "circle").lower()) or "circle"
    return (
        f'<span class="dashboard-lite-icon dashboard-lite-icon-{safe_semantic} dashboard-lite-icon-{safe_variant}" aria-hidden="true">'
        f'<svg viewBox="0 0 24 24" focusable="false">{path}</svg></span>'
    )


def _metric_card(
    label: str,
    value: Any,
    suffix: str = "",
    *,
    help_text: str = "",
    digits: int = 0,
    amount_unit: str = "",
    semantic_class: str = "neutral",
    display_text: str = "",
) -> None:
    display_value = str(display_text or "") or (
        _fmt_dashboard_amount(value, amount_unit) if amount_unit else _fmt_number(value, digits)
    )
    if display_value != "자료부족":
        display_value = f"{display_value}{suffix}"
    safe_label = html.escape(str(label or ""))
    safe_value = html.escape(str(display_value or ""))
    safe_help = html.escape(str(help_text or ""))
    safe_semantic = re.sub(r"[^a-z0-9_-]", "", str(semantic_class or "neutral").lower()) or "neutral"
    help_html = f'<div class="dashboard-lite-kpi-help">{safe_help}</div>' if safe_help else ""
    icon_name = {
        "current": "bars",
        "forecast": "trend",
        "judgement": "calendar",
        "remaining": "pie",
    }.get(safe_semantic)
    icon_html = _dashboard_inline_icon(icon_name, safe_semantic) if icon_name else ""
    st.markdown(
        f"""
        <div class="dashboard-lite-kpi-card dashboard-lite-kpi-{safe_semantic}">
          <div class="dashboard-lite-kpi-head">{icon_html}<div class="dashboard-lite-kpi-label">{safe_label}</div></div>
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
            position: relative;
            display: grid;
            grid-template-rows: auto 1fr auto;
            container-type: inline-size;
            border: 1px solid rgba(49, 51, 63, 0.18);
            border-radius: 11px;
            padding: 16px 18px 14px;
            height: 145px;
            box-sizing: border-box;
            overflow: hidden;
            background: #ffffff;
            box-shadow: 0 6px 18px rgba(15, 23, 42, 0.08);
        }
        .dashboard-lite-kpi-card::before {
            content: "";
            position: absolute;
            inset: 0 0 auto 0;
            height: 4px;
            background: var(--dashboard-kpi-accent, #64748b);
        }
        .dashboard-lite-kpi-current { --dashboard-kpi-accent: #2563eb; }
        .dashboard-lite-kpi-forecast { --dashboard-kpi-accent: #0f9f98; }
        .dashboard-lite-kpi-judgement { --dashboard-kpi-accent: #f97316; }
        .dashboard-lite-kpi-remaining { --dashboard-kpi-accent: #64748b; }
        .dashboard-lite-kpi-current .dashboard-lite-kpi-value { color: #1d4ed8; }
        .dashboard-lite-kpi-forecast .dashboard-lite-kpi-value { color: #0f9f98; }
        .dashboard-lite-kpi-judgement .dashboard-lite-kpi-value { color: #c2410c; }
        .dashboard-lite-kpi-remaining .dashboard-lite-kpi-value { color: #475569; }
        div[data-testid="stChatMessage"]:has([class*="st-key-dashboard_sales_progress__"]) {
            padding-left: 12px;
        }
        [class*="st-key-dashboard_sales_progress__"] h2 {
            margin-bottom: 0.4rem;
        }
        .dashboard-lite-kpi-label {
            color: rgba(49, 51, 63, 0.74);
            font-size: 0.9rem;
            line-height: 1.25;
            text-align: left;
            min-height: 1.25em;
            font-weight: 600;
        }
        .dashboard-lite-kpi-head {
            display: flex;
            align-items: center;
            gap: 11px;
            min-width: 0;
        }
        .dashboard-lite-icon {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            flex: 0 0 auto;
            color: #ffffff;
        }
        .dashboard-lite-icon svg {
            display: block;
            width: 58%;
            height: 58%;
        }
        .dashboard-lite-icon-circle {
            width: 38px;
            height: 38px;
            border: 1px solid rgba(255, 255, 255, 0.78);
            border-radius: 50%;
            box-shadow: 0 5px 10px rgba(15, 23, 42, 0.17), inset 0 1px 1px rgba(255, 255, 255, 0.34);
        }
        .dashboard-lite-icon-current { background: linear-gradient(145deg, #4f83ff, #2563eb); }
        .dashboard-lite-icon-forecast { background: linear-gradient(145deg, #2dd4c7, #0fa9a3); }
        .dashboard-lite-icon-judgement { background: linear-gradient(145deg, #ff9a4d, #f97316); }
        .dashboard-lite-icon-remaining { background: linear-gradient(145deg, #94a3b8, #64748b); }
        .dashboard-lite-icon-inventory { background: linear-gradient(145deg, #4f83ff, #2563eb); }
        .dashboard-lite-icon-frequency { background: linear-gradient(145deg, #2dd4c7, #0f9f98); }
        .dashboard-lite-icon-surge { background: linear-gradient(145deg, #a78bfa, #7c3aed); }
        .dashboard-lite-icon-vendor { background: linear-gradient(145deg, #fb7185, #dc2626); }
        .dashboard-lite-icon-compact {
            width: 19px;
            height: 19px;
            background: transparent;
            border: 0;
            box-shadow: none;
        }
        .dashboard-lite-icon-insight {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            box-shadow: 0 3px 8px rgba(15, 23, 42, 0.13), inset 0 1px 1px rgba(255, 255, 255, 0.30);
        }
        [class*="st-key-dashboard_inventory_summary__"] {
            margin-top: 10px;
        }
        [class*="st-key-dashboard_inventory_summary__"] [class*="st-key-dashboard_inventory_card__"] {
            --dashboard-inventory-accent: #64748b;
            position: relative;
            min-height: 382px;
            padding: 19px 21px 17px;
            border: 1px solid rgba(148, 163, 184, 0.25);
            border-radius: 18px;
            background: linear-gradient(145deg, rgba(255,255,255,0.99) 0%, rgba(248,250,252,0.94) 100%);
            box-shadow: 0 10px 24px rgba(15, 23, 42, 0.065), 0 2px 7px rgba(15, 23, 42, 0.045), inset 0 1px 0 rgba(255,255,255,0.94);
            box-sizing: border-box;
            overflow: hidden;
        }
        [class*="st-key-dashboard_inventory_summary__"] [class*="st-key-dashboard_inventory_card__"]::before {
            content: "";
            position: absolute;
            inset: 0 0 auto;
            height: 2px;
            background: linear-gradient(90deg, color-mix(in srgb, var(--dashboard-inventory-accent) 74%, white), color-mix(in srgb, var(--dashboard-inventory-accent) 28%, transparent));
        }
        [class*="st-key-dashboard_inventory_card__status__"] { --dashboard-inventory-accent: #2563eb; }
        [class*="st-key-dashboard_inventory_card__frequency__"] { --dashboard-inventory-accent: #0f9f98; }
        [class*="st-key-dashboard_inventory_card__surge__"] { --dashboard-inventory-accent: #7c3aed; }
        [class*="st-key-dashboard_inventory_card__vendor__"] { --dashboard-inventory-accent: #dc2626; }
        [class*="st-key-dashboard_inventory_row__"] [data-testid="stHorizontalBlock"] {
            align-items: stretch;
        }
        [class*="st-key-dashboard_inventory_row__"] [data-testid="stColumn"],
        [class*="st-key-dashboard_inventory_row__"] [data-testid="column"] {
            display: flex;
            flex-direction: column;
        }
        [class*="st-key-dashboard_inventory_row__"] [class*="st-key-dashboard_inventory_card__"] {
            flex: 1 1 auto;
            height: 100%;
        }
        [class*="st-key-dashboard_inventory_card__"] [class*="st-key-dashboard_inventory_card_body__"] {
            display: flex;
            flex: 1 1 auto;
            flex-direction: column;
            justify-content: center;
            min-height: 0;
        }
        [class*="st-key-dashboard_inventory_card__"] [class*="st-key-dashboard_inventory_card_body__"] > [data-testid="stHorizontalBlock"] {
            align-items: center;
        }
        .dashboard-lite-inventory-card-title {
            display: flex;
            align-items: center;
            gap: 9px;
            color: #1f2937;
            font-size: 1.05rem;
            font-weight: 700;
            line-height: 1.3;
        }
        .dashboard-lite-inventory-card-title { margin-top: 2px; }
        .dashboard-lite-inventory-card-title .dashboard-lite-icon-circle {
            width: 31px;
            height: 31px;
            box-shadow: 0 5px 11px color-mix(in srgb, var(--dashboard-inventory-accent) 28%, transparent), inset 0 1px 1px rgba(255,255,255,0.40);
        }
        .dashboard-lite-inventory-card-subtitle {
            margin: 6px 0 10px 40px;
            color: rgba(71, 85, 105, 0.76);
            font-size: 0.78rem;
            line-height: 1.4;
        }
        .dashboard-lite-inventory-legend {
            display: grid;
            gap: 4px;
            margin: 0;
        }
        .dashboard-lite-inventory-legend-row {
            display: grid;
            grid-template-columns: 10px max-content max-content;
            align-items: center;
            justify-content: start;
            column-gap: 8px;
            color: #475569;
            font-size: 0.83rem;
            line-height: 1.22;
        }
        .dashboard-lite-inventory-legend-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }
        .dashboard-lite-inventory-legend-value {
            color: #334155;
            font-weight: 650;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        .dashboard-lite-inventory-inner-panel {
            border: 1px solid rgba(226, 232, 240, 0.82);
            border-radius: 12px;
            padding: 7px 9px 5px;
            background: linear-gradient(145deg, rgba(255,255,255,0.94), rgba(241,245,249,0.76));
            box-shadow: inset 0 1px 2px rgba(15,23,42,0.035), 0 4px 10px rgba(15,23,42,0.035);
        }
        .dashboard-lite-inventory-status-layout {
            align-items: center;
            width: min(100%, 760px);
            margin: 0 auto;
        }
        .dashboard-lite-inventory-status-layout .dashboard-lite-inventory-legend {
            margin: -3px 0 0;
        }
        .dashboard-lite-demand-surge-flow {
            display: flex;
            align-items: center;
            gap: 7px;
            min-width: 0;
            margin: 0 0 3px;
            color: #6d28d9;
            font-size: 0.74rem;
            font-weight: 600;
            line-height: 1.2;
        }
        .dashboard-lite-demand-surge-flow-label {
            flex: 0 0 auto;
            white-space: nowrap;
        }
        .dashboard-lite-demand-surge-flow-arrow {
            display: block;
            min-width: 42px;
            width: 100%;
            height: 14px;
        }
        .dashboard-lite-demand-surge-detail-title {
            margin: 0 0 4px;
            color: #334155;
            font-size: 0.82rem;
            font-weight: 700;
            line-height: 1.3;
        }
        .dashboard-lite-demand-surge-detail-body {
            display: flex;
            flex: 1 1 auto;
            flex-direction: column;
            justify-content: center;
            min-height: 0;
        }
        [class*="st-key-dashboard_inventory_card__"] [data-testid="stAltairChart"] {
            margin: 0 !important;
            border-radius: 11px;
            background: transparent !important;
            box-shadow: none;
        }
        [class*="st-key-dashboard_inventory_card__"] [data-testid="stAltairChart"] > div,
        [class*="st-key-dashboard_inventory_card__"] [data-testid="stAltairChart"] svg,
        [class*="st-key-dashboard_inventory_card__"] [data-testid="stAltairChart"] canvas {
            background: transparent !important;
        }
        .dashboard-lite-inventory-card-footer {
            margin: 5px 0 0;
            color: rgba(71, 85, 105, 0.76);
            font-size: 0.78rem;
            line-height: 1.38;
        }
        @media (max-width: 760px) {
            [class*="st-key-dashboard_inventory_row__"] [data-testid="stHorizontalBlock"] {
                flex-direction: column;
            }
            [class*="st-key-dashboard_inventory_row__"] [data-testid="stColumn"],
            [class*="st-key-dashboard_inventory_row__"] [data-testid="column"] {
                width: 100% !important;
                flex: 1 1 auto !important;
            }
            [class*="st-key-dashboard_inventory_summary__"] [class*="st-key-dashboard_inventory_card__"] {
                min-height: 0;
                height: auto;
            }
            .dashboard-lite-inventory-legend-row {
                grid-template-columns: 10px minmax(0, 1fr) max-content;
            }
            .dashboard-lite-inventory-status-layout { width: 100%; }
            .dashboard-lite-demand-surge-flow { flex-wrap: wrap; }
            .dashboard-lite-demand-surge-flow-arrow { min-width: 120px; }
        }
        .dashboard-lite-kpi-value {
            align-self: center;
            margin: 4px 0 2px;
            text-align: center;
            font-size: 1.6rem;
            font-weight: 700;
            line-height: 1.2;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        .dashboard-lite-kpi-help {
            margin-top: 8px;
            color: rgba(49, 51, 63, 0.55);
            font-size: 0.75rem;
            text-align: left;
        }
        @container (max-width: 250px) {
            .dashboard-lite-kpi-value { font-size: 1.15rem; }
            .dashboard-lite-kpi-label { font-size: 0.82rem; }
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
            margin: 16px 0 8px;
            padding: 14px 18px 12px;
            border: 1px solid rgba(37, 99, 235, 0.16);
            border-radius: 11px;
            background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
            box-shadow: 0 5px 16px rgba(15, 23, 42, 0.06);
        }
        .dashboard-lite-sales-brief-row {
            display: grid;
            grid-template-columns: max-content minmax(0, 1.25fr) minmax(0, 1fr) minmax(0, 1fr);
            align-items: center;
            gap: 0;
        }
        .dashboard-lite-sales-brief-title {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin: 0 18px 0 0;
            padding: 9px 14px;
            border-radius: 999px;
            color: #ffffff;
            background: #172033;
            box-shadow: 0 4px 10px rgba(15, 23, 42, 0.16);
            font-size: 0.96rem;
            font-weight: 700;
            line-height: 1.2;
            white-space: nowrap;
        }
        .dashboard-lite-sales-brief-title svg {
            width: 18px;
            height: 18px;
            flex: 0 0 auto;
        }
        .dashboard-lite-sales-brief-line {
            margin: 0;
            padding: 4px 18px;
            border-left: 1px solid rgba(148, 163, 184, 0.34);
            display: flex;
            align-items: center;
            gap: 11px;
            color: rgba(31, 41, 55, 0.9);
            font-size: 0.88rem;
            line-height: 1.45;
        }
        .dashboard-lite-sales-brief-current strong { color: #1d4ed8; }
        .dashboard-lite-sales-brief-pace strong { color: #ea580c; }
        .dashboard-lite-sales-brief-prior strong { color: #0f9f98; }
        .dashboard-lite-sales-brief-note {
            margin: 9px 0 0 calc(18px + 9.8rem);
            color: rgba(49, 51, 63, 0.62);
            font-size: 0.76rem;
            line-height: 1.45;
        }
        [class*="st-key-dashboard_sales_gauge_card__"],
        [class*="st-key-dashboard_sales_chart_card__"] {
            min-height: 440px;
            box-sizing: border-box;
            padding: 18px 20px 20px;
            border: 1px solid rgba(49, 51, 63, 0.16);
            border-radius: 11px;
            background: #ffffff;
            box-shadow: 0 5px 16px rgba(15, 23, 42, 0.07);
        }
        .dashboard-lite-sales-gauge {
            min-height: 400px;
            display: flex;
            flex-direction: column;
            justify-content: flex-start;
            text-align: center;
        }
        .dashboard-lite-sales-gauge-title {
            color: #1f2937;
            font-size: 1.05rem;
            font-weight: 700;
            line-height: 1.35;
            text-align: left;
        }
        .dashboard-lite-sales-gauge svg {
            display: block;
            width: min(100%, 440px);
            margin: 12px auto -12px;
            overflow: visible;
        }
        .dashboard-lite-gauge-track {
            filter: drop-shadow(0 3px 4px rgba(15, 23, 42, 0.12));
        }
        .dashboard-lite-gauge-progress {
            filter: drop-shadow(0 5px 6px rgba(29, 78, 216, 0.27));
        }
        .dashboard-lite-gauge-needle {
            filter: drop-shadow(0 2px 2px rgba(30, 64, 175, 0.28));
        }
        .dashboard-lite-gauge-hub {
            filter: drop-shadow(0 4px 4px rgba(30, 64, 175, 0.32));
        }
        .dashboard-lite-gauge-main {
            color: #1f2937;
            font-size: 2.15rem;
            font-weight: 700;
            font-variant-numeric: tabular-nums;
        }
        .dashboard-lite-gauge-label, .dashboard-lite-gauge-sub {
            color: rgba(49, 51, 63, 0.68);
            font-size: 0.92rem;
            line-height: 1.55;
        }
        .dashboard-lite-gauge-label {
            color: #1e3a8a;
            font-weight: 650;
        }
        .dashboard-lite-gauge-sub {
            margin: 12px auto 0;
            padding-top: 11px;
            width: min(100%, 360px);
            border-top: 1px solid rgba(148, 163, 184, 0.28);
            color: #475569;
        }
        .dashboard-lite-sales-chart-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
            margin: 1px 2px 2px;
        }
        .dashboard-lite-sales-chart-title {
            color: #1f2937;
            font-size: 1.35rem;
            font-weight: 700;
            line-height: 1.3;
            white-space: nowrap;
        }
        .dashboard-lite-sales-chart-legend {
            display: flex;
            justify-content: flex-end;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px 20px;
            color: #334155;
            font-size: 0.84rem;
            font-weight: 650;
        }
        .dashboard-lite-sales-chart-legend-item {
            display: inline-flex;
            align-items: center;
            gap: 7px;
            white-space: nowrap;
        }
        .dashboard-lite-sales-chart-legend-swatch {
            width: 13px;
            height: 13px;
            border: 1px solid rgba(255, 255, 255, 0.9);
            border-radius: 3px;
            box-shadow: 0 1px 3px rgba(15, 23, 42, 0.18);
        }
        .dashboard-lite-sales-chart-legend-actual { background: linear-gradient(180deg, #4f83ff, #2563eb); }
        .dashboard-lite-sales-chart-legend-forecast { background: linear-gradient(180deg, #2dd4c7, #0fa9a3); }
        .dashboard-lite-sales-chart-legend-judgement { background: linear-gradient(180deg, #ff9a4d, #f97316); }
        @media (max-width: 1100px) {
            .dashboard-lite-sales-brief-row {
                grid-template-columns: 1fr;
                gap: 10px;
            }
            .dashboard-lite-sales-brief-title {
                width: fit-content;
                margin-right: 0;
            }
            .dashboard-lite-sales-brief-line {
                border-left-width: 3px;
            }
            .dashboard-lite-sales-brief-note {
                margin-left: 0;
            }
            .dashboard-lite-sales-chart-header {
                align-items: flex-start;
                flex-direction: column;
                gap: 8px;
            }
            .dashboard-lite-sales-chart-legend {
                justify-content: flex-start;
            }
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
    remaining = (metrics.get("current_month_remaining_forecast_sales") or {}).get("value")
    chart_rows = sales.get("chart_rows") or []
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
    evaluation_status = next(
        (status for status in ("완료월", "진행중", "미래월", "자료부족") if status in time_basis),
        "자료부족",
    )
    is_completed_month = evaluation_status == "완료월"
    historical_forecast_row = next(
        (
            row for row in chart_rows
            if str(row.get("kind") or "") == "완료월 사전예상"
            and str(row.get("period_sort") or "") == evaluation_month
        ),
        None,
    )
    historical_actual_row = next(
        (
            row for row in chart_rows
            if str(row.get("kind") or "") == "완료월 실제"
            and str(row.get("period_sort") or "") == evaluation_month
        ),
        None,
    )
    historical_forecast = historical_forecast_row.get("value") if historical_forecast_row else None
    historical_actual = historical_actual_row.get("value") if historical_actual_row else None
    completed_achievement = None
    completed_difference = None
    if is_completed_month:
        try:
            historical_actual_number = float(current_number)
        except (TypeError, ValueError):
            historical_actual_number = None
        try:
            historical_number = float(forecast_number)
        except (TypeError, ValueError):
            historical_number = None
        if historical_actual_number is None or historical_number is None or historical_number <= 0:
            comparison_label, comparison_amount = "완료월 예상 비교 자료 없음", None
        else:
            completed_difference = historical_actual_number - historical_number
            completed_achievement = historical_actual_number / historical_number * 100.0
            if completed_difference > 0:
                comparison_label, comparison_amount = "예상보다 초과", completed_difference
            elif completed_difference < 0:
                comparison_label, comparison_amount = "예상보다 미달", abs(completed_difference)
            else:
                comparison_label, comparison_amount = "예상과 동일", 0.0
    day_match = re.search(r"(\d+)\s*/\s*(\d+)", time_basis)
    if day_match:
        elapsed_days, total_days = int(day_match.group(1)), int(day_match.group(2))
    elif evaluation_month and time_progress is not None:
        try:
            total_days = monthrange(int(evaluation_month[:4]), int(evaluation_month[4:6]))[1]
            elapsed_days = round(total_days * float(time_progress) / 100.0)
        except (ValueError, TypeError):
            elapsed_days = total_days = None

    judgement_digits = re.sub(r"\D", "", str((facts.get("period") or {}).get("judgement_date") or ""))[:8]
    judgement_day = None
    if len(judgement_digits) == 8 and (not evaluation_month or judgement_digits[:6] == evaluation_month):
        judgement_day = int(judgement_digits[-2:])
    elif elapsed_days not in (None, 0):
        judgement_day = int(elapsed_days)
    expected_to_date_value = visualization.get("expected_to_date_sales")
    expected_available = expected_to_date_value is not None
    try:
        expected_duplicates_month_end = (
            expected_available
            and forecast_number is not None
            and math.isclose(float(expected_to_date_value), forecast_number, rel_tol=1e-9, abs_tol=0.01)
        )
    except (TypeError, ValueError):
        expected_duplicates_month_end = False
    expected_chart_visible = (
        expected_available
        and time_progress not in (None, 0)
        and not expected_duplicates_month_end
    )
    judgement_basis = f"{judgement_day}일 기준" if judgement_day else "판단 기준일"
    evaluation_label = f"{int(evaluation_month[4:6])}월" if len(evaluation_month) == 6 else "평가월"

    return {
        "current_sales": current,
        "forecast_sales": forecast,
        "expected_to_date_sales": expected_to_date_value,
        "remaining_forecast_sales": remaining if remaining is not None else visualization.get("remaining_forecast"),
        "sales_progress_pct": (
            completed_achievement if is_completed_month
            else visualization.get("sales_progress_pct", (metrics.get("current_month_progress_pct") or {}).get("value"))
        ),
        "time_progress_pct": time_progress,
        "time_adjusted_achievement_pct": achievement,
        "time_adjusted_status": _sales_time_status_label(achievement),
        "comparison_label": comparison_label,
        "comparison_amount": comparison_amount,
        "elapsed_days": elapsed_days,
        "total_days": total_days,
        "judgement_day": judgement_day,
        "judgement_basis": judgement_basis,
        "expected_to_date_available": expected_available,
        "expected_to_date_chart_visible": expected_chart_visible,
        "expected_to_date_duplicates_month_end": expected_duplicates_month_end,
        "evaluation_label": evaluation_label,
        "evaluation_status": evaluation_status,
        "is_completed_month": is_completed_month,
        "historical_forecast_available": historical_forecast_row is not None,
        "historical_actual_available": historical_actual_row is not None,
        "completed_forecast_achievement_pct": completed_achievement,
        "completed_forecast_difference": completed_difference,
        "completed_actual_source_table": (historical_actual_row or {}).get("source_table") or visualization.get("evaluation_actual_source_table"),
        "completed_actual_cutoff_date": (historical_actual_row or {}).get("cutoff_date") or visualization.get("evaluation_actual_cutoff_date"),
        "completed_forecast_helper": (historical_forecast_row or {}).get("forecast_helper") or visualization.get("evaluation_forecast_helper"),
    }


def _sales_gauge_state(facts: dict[str, Any]) -> dict[str, Any]:
    """Return display-only gauge bounds; current-day progress stays calendar based."""
    state = _sales_presentation_state(facts)
    gauge_available = state.get("sales_progress_pct") is not None
    try:
        progress = max(0.0, float(state.get("sales_progress_pct"))) if gauge_available else None
    except (TypeError, ValueError):
        progress = None
        gauge_available = False
    try:
        today_progress = max(0.0, float(state.get("time_adjusted_achievement_pct") or 0.0))
    except (TypeError, ValueError):
        today_progress = 0.0
    maximum = max(120.0, min(max(progress or 0.0, today_progress, 100.0) + 10.0, 250.0))
    return {
        **state,
        "gauge_available": gauge_available,
        "gauge_max_pct": maximum,
        "needle_pct": progress,
        "today_marker_pct": today_progress,
        "time_basis_label": "판단 기준일",
    }


def _build_sales_gauge_markup(facts: dict[str, Any], *, render_namespace: str = "sales") -> str:
    """Build one self-contained SVG without Markdown-breaking empty fragments."""
    state = _sales_gauge_state(facts)
    gauge_available = bool(state["gauge_available"])
    progress = float(state["needle_pct"] or 0.0)
    visual_progress = max(0.0, min(progress, 100.0))
    progress_arc_pct = visual_progress
    needle_angle = -90.0 + visual_progress * 1.8
    target_angle = 90.0
    today_visual_pct = max(0.0, min(float(state["today_marker_pct"]), 100.0))
    today_angle = -90.0 + today_visual_pct * 1.8
    today_marker = (
        f'<path d="M 140 20 L 140 42" stroke="#f97316" stroke-width="5" stroke-linecap="round" '
        f'transform="rotate({today_angle:.3f} 140 132)"/>'
        if gauge_available and state["expected_to_date_chart_visible"] else ""
    )
    today_achievement = (
        f"{html.escape(_fmt_number(state['today_marker_pct'], 1))}%"
        if state["expected_to_date_available"] else "자료부족"
    )
    safe_svg_namespace = re.sub(r"[^a-zA-Z0-9_-]", "", str(render_namespace or "sales")) or "sales"
    track_gradient_id = f"dashboardGaugeTrack-{safe_svg_namespace}"
    progress_gradient_id = f"dashboardGaugeProgress-{safe_svg_namespace}"
    needle_gradient_id = f"dashboardGaugeNeedle-{safe_svg_namespace}"
    hub_gradient_id = f"dashboardGaugeHub-{safe_svg_namespace}"
    shadow_filter_id = f"dashboardGaugeShadow-{safe_svg_namespace}"
    needle_filter_id = f"dashboardGaugeNeedleShadow-{safe_svg_namespace}"
    svg_parts = [
        f'<svg viewBox="0 0 280 168" role="img" aria-label="{html.escape(state["evaluation_label"])} 매출 진행">',
        "<defs>",
        f'<linearGradient id="{track_gradient_id}" gradientUnits="userSpaceOnUse" x1="140" y1="18" x2="140" y2="148"><stop offset="0%" stop-color="#ffffff"/><stop offset="28%" stop-color="#f4f7fb"/><stop offset="62%" stop-color="#e1e7ef"/><stop offset="100%" stop-color="#c8d2df"/></linearGradient>',
        f'<linearGradient id="{progress_gradient_id}" gradientUnits="userSpaceOnUse" x1="140" y1="18" x2="140" y2="148"><stop offset="0%" stop-color="#b9d4ff"/><stop offset="14%" stop-color="#78a9ff"/><stop offset="34%" stop-color="#4f83f5"/><stop offset="66%" stop-color="#2f6fe5"/><stop offset="88%" stop-color="#2058ca"/><stop offset="100%" stop-color="#173f9f"/></linearGradient>',
        f'<linearGradient id="{needle_gradient_id}" gradientUnits="userSpaceOnUse" x1="136" y1="43" x2="144" y2="43"><stop offset="0%" stop-color="#173f9c"/><stop offset="42%" stop-color="#6fa2ff"/><stop offset="62%" stop-color="#3b7af0"/><stop offset="100%" stop-color="#12327c"/></linearGradient>',
        f'<radialGradient id="{hub_gradient_id}" cx="38%" cy="28%" r="75%"><stop offset="0%" stop-color="#dce9ff"/><stop offset="25%" stop-color="#78a8ff"/><stop offset="58%" stop-color="#2f6fed"/><stop offset="100%" stop-color="#102f78"/></radialGradient>',
        f'<filter id="{shadow_filter_id}" x="-24%" y="-24%" width="148%" height="160%"><feDropShadow dx="0" dy="4" stdDeviation="3.8" flood-color="#123b92" flood-opacity="0.34"/></filter>',
        f'<filter id="{needle_filter_id}" x="-80%" y="-24%" width="260%" height="160%"><feDropShadow dx="0" dy="3" stdDeviation="2.4" flood-color="#0f255f" flood-opacity="0.38"/></filter>',
        "</defs>",
        f'<path class="dashboard-lite-gauge-track" d="M 30 132 A 110 110 0 0 1 250 132" fill="none" stroke="url(#{track_gradient_id})" stroke-width="24" stroke-linecap="round"/>',
    ]
    if gauge_available:
        svg_parts.extend([
            f'<path class="dashboard-lite-gauge-progress" d="M 30 132 A 110 110 0 0 1 250 132" pathLength="100" fill="none" stroke="url(#{progress_gradient_id})" stroke-width="24" stroke-linecap="round" stroke-dasharray="{progress_arc_pct:.3f} 100" filter="url(#{shadow_filter_id})"/>',
            f'<path d="M 140 20 L 140 42" stroke="#0f766e" stroke-width="4" stroke-linecap="round" transform="rotate({target_angle:.3f} 140 132)"/>',
        ])
        if today_marker:
            svg_parts.append(today_marker)
        svg_parts.extend([
            f'<rect class="dashboard-lite-gauge-needle" x="136.5" y="43" width="7" height="89" rx="3.5" fill="url(#{needle_gradient_id})" stroke="#1e3a8a" stroke-width="0.8" filter="url(#{needle_filter_id})" transform="rotate({needle_angle:.3f} 140 132)"/>',
            f'<g class="dashboard-lite-gauge-hub" filter="url(#{needle_filter_id})"><circle cx="140" cy="132" r="14" fill="#0f2f7c"/><circle cx="140" cy="132" r="11" fill="#2563eb"/><circle cx="140" cy="132" r="8.6" fill="url(#{hub_gradient_id})"/><circle cx="140" cy="132" r="4.4" fill="#f8fbff" stroke="#dbeafe" stroke-width="0.8"/><circle cx="138.4" cy="130.2" r="1.7" fill="#ffffff" opacity="0.90"/></g>',
        ])
    svg_parts.extend([
        '<text x="28" y="160" text-anchor="middle" fill="#64748b" font-size="12">0%</text>',
        '<text x="252" y="160" text-anchor="middle" fill="#64748b" font-size="12">100%</text>',
        "</svg>",
    ])
    if not gauge_available:
        main_text = "자료부족"
        gauge_label = "비교 자료 없음" if state["is_completed_month"] else "예상매출 달성률 계산 불가"
        gauge_sub = (
            "평가월의 공식 예상매출 자료를 확인해 주세요."
            if state["is_completed_month"] else "평가월의 현재매출·월말 예상 자료를 확인해 주세요."
        )
    else:
        main_text = f"{_fmt_number(progress, 1)}%"
        gauge_label = "예상매출 대비 달성률" if state["is_completed_month"] else "예상매출 달성률"
        if state["is_completed_month"]:
            amount_unit = _facts_amount_display_unit(facts)
            difference = state.get("completed_forecast_difference")
            if difference is None:
                gauge_sub = "비교 자료 없음"
            elif difference > 0:
                gauge_sub = f"예상보다 {_fmt_dashboard_amount(abs(difference), amount_unit)} 초과"
            elif difference < 0:
                gauge_sub = f"예상보다 {_fmt_dashboard_amount(abs(difference), amount_unit)} 미달"
            else:
                gauge_sub = "예상과 동일"
        else:
            gauge_sub = f"{state['judgement_basis']} 예상 대비 달성률 {today_achievement}"
    return "".join([
        '<div class="dashboard-lite-sales-gauge">',
        f'<div class="dashboard-lite-sales-gauge-title">{html.escape(state["evaluation_label"])} 매출 진행</div>',
        "".join(svg_parts),
        f'<div class="dashboard-lite-gauge-main">{html.escape(main_text)}</div>',
        f'<div class="dashboard-lite-gauge-label">{html.escape(gauge_label)}</div>',
        f'<div class="dashboard-lite-gauge-sub">{html.escape(gauge_sub)}</div>',
        "</div>",
    ])


def _render_sales_gauge(facts: dict[str, Any], *, render_namespace: str = "sales") -> None:
    _inject_dashboard_lite_styles_once()
    st.markdown(
        _build_sales_gauge_markup(facts, render_namespace=render_namespace),
        unsafe_allow_html=True,
    )


def _render_status_cards(facts: dict[str, Any]) -> None:
    _inject_dashboard_lite_styles_once()
    state = _sales_presentation_state(facts)
    amount_unit = _facts_amount_display_unit(facts)
    cards = [
        ("현재매출", state["current_sales"], "평가월 말일 확정 실적" if state["is_completed_month"] else "평가월 누적 실적", "amount", "current", ""),
        (
            "월말 예상매출",
            state["forecast_sales"],
            "완료월 공식 사전예상" if state["is_completed_month"] and state["historical_forecast_available"] else ("비교 자료 없음" if state["is_completed_month"] else "당월 월말 예상"),
            "amount",
            "forecast",
            "비교 자료 없음" if state["is_completed_month"] and not state["historical_forecast_available"] else "",
        ),
        (
            f"{state['judgement_basis']} 예상매출",
            state["expected_to_date_sales"],
            "평가월 말일 기준" if state["is_completed_month"] else ("월 경과율 반영" if state["expected_to_date_available"] else "경계월 계산 제외"),
            "amount",
            "judgement",
            "자료부족" if state["expected_to_date_sales"] is None else "",
        ),
        (
            "월말 예상 잔여",
            state["remaining_forecast_sales"],
            "공식 잔여예상",
            "amount",
            "remaining",
            "자료부족" if state["remaining_forecast_sales"] is None else "",
        ),
    ]
    if amount_unit == "auto":
        amount_values = [value for _label, value, _help, kind, _semantic, _display in cards if kind == "amount"]
        try:
            max_amount = max(abs(float(value)) for value in amount_values if value is not None)
        except ValueError:
            max_amount = 0.0
        divisor, _label = _amount_display_spec("auto", max_amount)
        amount_unit = "million" if divisor == 1_000_000 else ("thousand" if divisor == 1_000 else "won")

    cols = st.columns(4)
    for col, (label, value, help_text, kind, semantic_class, display_text) in zip(cols, cards):
        with col:
            _metric_card(
                label,
                value,
                "%" if kind == "pct" else "",
                help_text=help_text,
                digits=1 if kind == "pct" else 0,
                amount_unit=amount_unit if kind == "amount" else "",
                semantic_class=semantic_class,
                display_text=display_text,
            )



def _render_sales_brief(facts: dict[str, Any]) -> None:
    sales = facts.get("sales") or {}
    metrics = sales.get("metrics") or {}
    visualization = sales.get("visualization") or {}
    current = metrics.get("current_month_sales") or {}
    forecast = metrics.get("current_month_forecast_sales") or {}
    state = _sales_presentation_state(facts)
    amount_unit = _facts_amount_display_unit(facts)
    current_value = state.get("current_sales", current.get("value"))
    forecast_value = state.get("forecast_sales", forecast.get("value"))
    if current_value is None:
        st.caption("당월 현재·예상 매출 자료가 부족합니다.")
        return

    if state["is_completed_month"]:
        if state["historical_forecast_available"] and forecast_value is not None:
            difference = state.get("completed_forecast_difference")
            achievement = state.get("completed_forecast_achievement_pct")
            if difference is None:
                difference_line = "예상 대비 차이 자료가 없습니다."
            elif difference > 0:
                difference_line = f"공식 예상보다 <strong>{html.escape(_fmt_dashboard_amount(abs(difference), amount_unit))}</strong> 초과했습니다."
            elif difference < 0:
                difference_line = f"공식 예상보다 <strong>{html.escape(_fmt_dashboard_amount(abs(difference), amount_unit))}</strong> 미달했습니다."
            else:
                difference_line = "공식 예상과 동일한 실적입니다."
            lines = [
                f"평가월 확정 실제매출은 <strong>{html.escape(_fmt_dashboard_amount(current_value, amount_unit))}</strong>이며, 공식 예상매출은 <strong>{html.escape(_fmt_dashboard_amount(forecast_value, amount_unit))}</strong>입니다.",
                difference_line,
                f"예상 대비 달성률은 <strong>{html.escape(_fmt_number(achievement, 1))}%</strong>입니다.",
            ]
        else:
            lines = [
                f"평가월 확정 실제매출은 <strong>{html.escape(_fmt_dashboard_amount(current_value, amount_unit))}</strong>입니다.",
                "공식 예상매출 비교 자료가 없습니다.",
                "기준일 예상매출 비교 자료가 없습니다.",
            ]
    elif state["comparison_label"] == "월말 예상 초과":
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

    if not state["is_completed_month"]:
        expected_to_date = state["expected_to_date_sales"]
        if expected_to_date is None:
            lines.append("판단 기준일 예상매출을 계산할 수 있는 평가월 자료가 부족합니다.")
        else:
            difference = float(current_value) - float(expected_to_date)
            if state["time_adjusted_status"] == "현재일 예상과 유사":
                lines.append("판단 기준일 예상매출과 유사한 수준입니다.")
            elif difference > 0:
                lines.append(f"판단 기준일 예상매출보다 <strong>{html.escape(_fmt_dashboard_amount(abs(difference), amount_unit))}</strong> 앞서 있습니다.")
            else:
                lines.append(f"판단 기준일 예상매출보다 <strong>{html.escape(_fmt_dashboard_amount(abs(difference), amount_unit))}</strong> 뒤처져 있습니다.")

    chart_rows = sales.get("chart_rows") or []
    completed = [row for row in chart_rows if row.get("kind") == "완료월 실제"]
    preforecast = [row for row in chart_rows if row.get("kind") == "완료월 사전예상"]
    preforecast_by_month = {str(row.get("period_sort") or ""): row for row in preforecast}
    comparable = [row for row in completed if str(row.get("period_sort") or "") in preforecast_by_month]
    if comparable and not state["is_completed_month"]:
        latest = max(comparable, key=lambda row: str(row.get("period_sort") or ""))
        prior = preforecast_by_month[str(latest.get("period_sort") or "")]
        prior_value = float(prior.get("value") or 0)
        if prior_value:
            delta_pct = (float(latest.get("value") or 0) / prior_value - 1.0) * 100.0
            direction = "높았습니다" if delta_pct > 0 else ("낮았습니다" if delta_pct < 0 else "같았습니다")
            lines.append(f"최근 완료월 실적은 당시 사전예상보다 <strong>{html.escape(_fmt_number(abs(delta_pct), 1))}%</strong> {direction}.")
        else:
            lines.append("최근 완료월의 비교 가능한 사전예상 자료가 없습니다.")
    elif not state["is_completed_month"]:
        lines.append("최근 완료월의 비교 가능한 사전예상 자료가 없습니다.")
    elif len(lines) < 3:
        lines.append("완료월 실적은 월별 실제매출 차트에 그대로 유지됩니다.")
    title_icon = _dashboard_inline_icon("report", "remaining", variant="compact")
    insight_html = "".join(
        f'<div class="dashboard-lite-sales-brief-line dashboard-lite-sales-brief-{semantic}">'
        f'{_dashboard_inline_icon(icon_name, icon_semantic, variant="insight")}<span>{line}</span></div>'
        for semantic, icon_name, icon_semantic, line in zip(
            ("current", "pace", "prior"),
            ("trend", "compare", "target"),
            ("current", "judgement", "forecast"),
            lines,
        )
    )
    st.markdown(
        "<div class=\"dashboard-lite-sales-brief\">"
        "<div class=\"dashboard-lite-sales-brief-row\">"
        f"<div class=\"dashboard-lite-sales-brief-title\">{title_icon}<span>오늘의 매출 요약</span></div>"
        + insight_html
        + "</div>"
        + "<div class=\"dashboard-lite-sales-brief-note\">기준일 예상매출은 평가월 경과율을 반영하며, 완료월은 말일 기준입니다.</div>"
        + "</div>",
        unsafe_allow_html=True,
    )


def _build_sales_bar_chart(facts: dict[str, Any]) -> alt.Chart | alt.LayerChart | None:
    """Build grouped actual, month-end forecast, and eligible current-day bars."""
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
    forecast_df["series"] = "월말 예상매출"
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
    visualization = (facts.get("sales") or {}).get("visualization") or {}
    state = _sales_presentation_state(facts)
    current_day_series = f"{state['judgement_basis']} 예상매출"
    display_frames = [actual_df, forecast_df]
    if state["expected_to_date_chart_visible"]:
        marker_period = str(visualization.get("evaluation_month") or "")
        marker_period_label = (
            period_display_map.get(f"{marker_period[:4]}-{marker_period[4:6]}")
            or state["evaluation_label"]
        )
        display_frames.append(pd.DataFrame([{
            "display_period": marker_period_label,
            "display_value": float(state["expected_to_date_sales"]) / divisor,
            "value": float(state["expected_to_date_sales"]),
            "series": current_day_series,
            "value_kind": current_day_series,
            "amount_display_unit": unit_label,
            "month_status": "평가월",
            "time_progress_pct": visualization.get("time_progress_pct"),
        }]))
    display_df = pd.concat(display_frames, ignore_index=True, sort=False)
    cluster_order = ["실제매출", "월말 예상매출", current_day_series]
    legend_order = ["실제매출", "월말 예상매출", current_day_series]
    x_encoding = alt.X(
        "display_period:N",
        title=None,
        sort=period_order,
        scale=alt.Scale(paddingInner=0.42, paddingOuter=0.10),
        axis=alt.Axis(labelAngle=0, labelPadding=10, labelFontSize=13),
    )
    x_offset = alt.XOffset(
        "series:N",
        sort=cluster_order,
        scale=alt.Scale(domain=cluster_order, paddingInner=0.03, paddingOuter=0.02),
    )
    y_encoding = alt.Y(
        "display_value:Q",
        title=f"매출 ({unit_label})",
        stack=None,
        axis=alt.Axis(
            grid=True,
            gridColor="#e5e7eb",
            gridOpacity=0.8,
            labelFontSize=12,
            titleFontSize=13,
            titlePadding=12,
        ),
    )
    gradient_specs = {
        "실제매출": (["#1d4ed8", "#75a6ff", "#3f7cf4", "#1745b8"], "#1745b8"),
        "월말 예상매출": (["#0b8f8a", "#63e0d7", "#25c4bb", "#087b77"], "#087b77"),
        current_day_series: (["#dc5a0c", "#ffb06f", "#ff8738", "#c94b05"], "#c94b05"),
    }
    bar_layers: list[alt.Chart] = []
    for series_name in cluster_order:
        series_frame = display_df[display_df["series"] == series_name]
        if series_frame.empty:
            continue
        gradient_colors, border_color = gradient_specs[series_name]
        gradient = alt.Gradient(
            gradient="linear",
            x1=0,
            y1=0,
            x2=1,
            y2=0,
            stops=[
                alt.GradientStop(offset=0, color=gradient_colors[0]),
                alt.GradientStop(offset=0.34, color=gradient_colors[1]),
                alt.GradientStop(offset=0.66, color=gradient_colors[2]),
                alt.GradientStop(offset=1, color=gradient_colors[3]),
            ],
        )
        bar_layers.append(
            alt.Chart(series_frame).mark_bar(
                color=gradient,
                opacity=0.96,
                cornerRadiusTopLeft=5,
                cornerRadiusTopRight=5,
                stroke=border_color,
                strokeWidth=0.8,
            ).encode(
                x=x_encoding,
                xOffset=x_offset,
                y=y_encoding,
                tooltip=tooltip,
            )
        )
    bars = alt.layer(*bar_layers)
    if not state["expected_to_date_chart_visible"]:
        return bars.properties(
            height=370,
            padding={"left": 8, "right": 16, "top": 18, "bottom": 24},
        )
    label_df = display_df[display_df["series"] == current_day_series]
    labels = alt.Chart(label_df).mark_text(
        dy=-10,
        align="center",
        baseline="bottom",
        color="#9a3412",
        fontSize=11,
        fontWeight=600,
    ).encode(
        x=x_encoding,
        xOffset=x_offset,
        y=y_encoding,
        text=alt.Text("display_value:Q", format=",.0f"),
    )
    return alt.layer(bars, labels).resolve_scale(y="shared").properties(
        height=370,
        padding={"left": 8, "right": 16, "top": 18, "bottom": 24},
    )


def _render_sales_chart(facts: dict[str, Any]) -> None:
    state = _sales_presentation_state(facts)
    chart = _build_sales_bar_chart(facts)
    if chart is None:
        st.info("매출 그래프를 표시할 완료월/당월 facts가 없습니다.")
        return
    judgement_label = f"{state['judgement_basis']} 예상매출"
    judgement_legend = (
        f'<span class="dashboard-lite-sales-chart-legend-item"><span class="dashboard-lite-sales-chart-legend-swatch dashboard-lite-sales-chart-legend-judgement"></span>{html.escape(judgement_label)}</span>'
        if state["expected_to_date_chart_visible"] else ""
    )
    st.markdown(
        "<div class=\"dashboard-lite-sales-chart-header\">"
        "<div class=\"dashboard-lite-sales-chart-title\">월별 실제매출·예상매출</div>"
        "<div class=\"dashboard-lite-sales-chart-legend\">"
        "<span class=\"dashboard-lite-sales-chart-legend-item\"><span class=\"dashboard-lite-sales-chart-legend-swatch dashboard-lite-sales-chart-legend-actual\"></span>실제매출</span>"
        "<span class=\"dashboard-lite-sales-chart-legend-item\"><span class=\"dashboard-lite-sales-chart-legend-swatch dashboard-lite-sales-chart-legend-forecast\"></span>월말 예상매출</span>"
        + judgement_legend
        + "</div></div>",
        unsafe_allow_html=True,
    )
    st.altair_chart(chart, width="stretch")


def _build_stock_readiness_chart(facts: dict[str, Any]) -> alt.Chart | alt.LayerChart | None:
    inventory = facts.get("inventory") or {}
    rows = inventory.get("risk_targets") or []
    threshold_value = _dashboard_readiness_threshold(facts)
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
    threshold_value = _dashboard_readiness_threshold(facts)
    threshold_text = _fmt_threshold_pct(threshold_value)
    chart = _build_stock_readiness_chart(facts)
    if chart is None:
        st.info(f"준비율 경고기준 {threshold_text}% 미만 재고준비율 조치 대상이 없습니다.")
        return
    st.altair_chart(chart, width="stretch")


def _inventory_status_summary(facts: Mapping[str, Any]) -> Mapping[str, Any]:
    return ((facts.get("inventory") or {}).get("inventory_status_summary") or {})


def _build_inventory_status_chart(facts: dict[str, Any]) -> alt.Chart | None:
    summary = _inventory_status_summary(facts)
    counts = summary.get("status_counts") or {}
    rows = [
        {"label": "긴급 부족", "count": int(counts.get("긴급 부족") or 0), "amount": 0.0},
        {"label": "재고 경고", "count": int(counts.get("재고 경고") or 0), "amount": 0.0},
        {"label": "적정 재고", "count": int(counts.get("적정 재고") or 0), "amount": 0.0},
        {"label": "과다 재고", "count": int(counts.get("과다 재고") or 0), "amount": 0.0},
        {"label": "예상수요 없음", "count": int(counts.get("예상수요 없음") or 0), "amount": 0.0},
        {"label": "자료 부족", "count": int(counts.get("자료 부족") or 0), "amount": 0.0},
    ]
    return _build_count_donut(
        rows,
        total_label="관리 품목",
        total=int(summary.get("total_product_count") or 0),
        colors=["#dc2626", "#f59e0b", "#16a34a", "#7c3aed", "#94a3b8", "#64748b"],
    )


def _inventory_status_rows(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    summary = _inventory_status_summary(facts)
    counts = summary.get("status_counts") or {}
    return [
        {"label": label, "count": int(counts.get(label) or 0), "color": color, "text_color": text_color}
        for label, color, text_color in (
            ("긴급 부족", "#dc2626", "#ffffff"),
            ("재고 경고", "#f59e0b", "#ffffff"),
            ("적정 재고", "#16a34a", "#ffffff"),
            ("과다 재고", "#7c3aed", "#ffffff"),
            ("예상수요 없음", "#94a3b8", "#1f2937"),
            ("자료 부족", "#64748b", "#ffffff"),
        )
    ]


def _build_labeled_summary_donut(
    rows: list[dict[str, Any]],
    *,
    total_label: str,
    total: int,
    height: int = 212,
    inner_radius: int = 58,
    outer_radius: int = 92,
    top_padding: int = 12,
) -> alt.Chart | None:
    """Render persisted counts with labels tied to the original pie geometry."""
    frame = pd.DataFrame([dict(row) for row in rows if int(row.get("count") or 0) > 0])
    if frame.empty:
        return None
    positive_total = int(frame["count"].sum())
    if total <= 0 or positive_total != int(total):
        return None
    frame["pct"] = frame["count"].map(lambda value: float(value) / total * 100.0)
    frame["pct_label"] = frame["pct"].map(lambda value: f"{value:.1f}%")
    # All rings and labels consume these exact cumulative angles.  Text layers
    # must not stack a filtered subset of pie rows, or labels move to a different arc.
    cursor = 0.0
    geometries: list[dict[str, float]] = []
    for _, row in frame.iterrows():
        start_angle = cursor
        end_angle = cursor + math.tau * float(row["count"]) / float(total)
        geometries.append({"start_angle": start_angle, "end_angle": end_angle, "mid_angle": (start_angle + end_angle) / 2.0})
        cursor = end_angle
    geometry = pd.DataFrame(geometries, index=frame.index)
    frame = pd.concat([frame, geometry], axis=1)
    frame["inside_label"] = frame.apply(
        lambda row: row["pct_label"] if float(row["pct"]) >= 7.0 else "", axis=1
    )
    frame["outside_label"] = frame.apply(
        lambda row: row["pct_label"] if 0.0 < float(row["pct"]) < 7.0 else "", axis=1
    )
    color_domain = [str(row["label"]) for row in rows]
    color_range = [str(row["color"]) for row in rows]
    angle_encoding = {
        "theta": alt.Theta("start_angle:Q", stack=None),
        "theta2": alt.Theta2("end_angle:Q"),
    }
    shadow = alt.Chart(frame).mark_arc(innerRadius=outer_radius, outerRadius=outer_radius + 5, cornerRadius=3, color="#0f172a", opacity=0.075).encode(
        **angle_encoding,
    )
    arc = alt.Chart(frame).mark_arc(innerRadius=inner_radius, outerRadius=outer_radius, cornerRadius=3).encode(
        **angle_encoding,
        color=alt.Color("label:N", legend=None, scale=alt.Scale(domain=color_domain, range=color_range)),
        tooltip=[
            alt.Tooltip("label:N", title="상태"),
            alt.Tooltip("count:Q", title="품목 수", format=",.0f"),
            alt.Tooltip("pct:Q", title="비율", format=".1f"),
        ],
    )
    outer_rim = alt.Chart(frame).mark_arc(innerRadius=outer_radius - 3, outerRadius=outer_radius, cornerRadius=3, color="#ffffff", opacity=0.16).encode(
        **angle_encoding,
    )
    inner_rim = alt.Chart(frame).mark_arc(innerRadius=inner_radius, outerRadius=inner_radius + 3, cornerRadius=2, color="#0f172a", opacity=0.075).encode(
        **angle_encoding,
    )
    layers: list[alt.Chart] = [shadow, arc, outer_rim, inner_rim]
    layers.append(
        alt.Chart(frame).mark_text(radius=(inner_radius + outer_radius) // 2, fontSize=11, fontWeight=700).encode(
            theta=alt.Theta("mid_angle:Q", stack=None),
            text="inside_label:N",
            color=alt.Color("text_color:N", scale=None),
        )
    )
    layers.append(
        alt.Chart(frame).mark_text(radius=outer_radius + 16, fontSize=10, fontWeight=600).encode(
            theta=alt.Theta("mid_angle:Q", stack=None),
            text="outside_label:N",
            color=alt.Color("color:N", scale=None),
        )
    )
    center = pd.DataFrame([{"total": f"{int(total):,}개", "label": total_label}])
    layers.extend([
        alt.Chart(center).mark_text(fontSize=23 if outer_radius >= 100 else 21, fontWeight=700, dy=-8, color="#1f2937").encode(text="total:N"),
        alt.Chart(center).mark_text(fontSize=12 if outer_radius >= 100 else 11, dy=15, color="#64748b").encode(text="label:N"),
    ])
    # Keep the ring geometry unchanged while reserving a small top inset for
    # outer percentage labels. Both inventory-status and demand-surge donuts
    # use this component, so their visual center stays aligned.
    return alt.layer(*layers).properties(
        height=height,
        padding={"top": max(0, int(top_padding)), "bottom": 0, "left": 0, "right": 0},
    ).configure(background="transparent").configure_view(stroke=None)


def _render_summary_card_heading(icon: str, semantic: str, title: str, subtitle: str) -> None:
    st.markdown(
        f'<div class="dashboard-lite-inventory-card-title">{_dashboard_inline_icon(icon, semantic)}'
        f'<span>{html.escape(title)}</span></div>'
        f'<div class="dashboard-lite-inventory-card-subtitle">{html.escape(subtitle)}</div>',
        unsafe_allow_html=True,
    )


def _render_inventory_summary_legend(rows: list[dict[str, Any]], *, total: int) -> None:
    items: list[str] = []
    for row in rows:
        count = int(row.get("count") or 0)
        pct = (count / total * 100.0) if total else 0.0
        items.append(
            '<div class="dashboard-lite-inventory-legend-row">'
            f'<span class="dashboard-lite-inventory-legend-dot" style="background:{html.escape(str(row["color"]))}"></span>'
            f'<span>{html.escape(str(row["label"]))}</span>'
            f'<span class="dashboard-lite-inventory-legend-value">{count:,}개 · {pct:.1f}%</span>'
            '</div>'
        )
    st.markdown('<div class="dashboard-lite-inventory-legend">' + ''.join(items) + '</div>', unsafe_allow_html=True)


def _build_inventory_status_summary_chart(facts: dict[str, Any]) -> alt.Chart | None:
    summary = _inventory_status_summary(facts)
    return _build_labeled_summary_donut(
        _inventory_status_rows(facts),
        total_label="전체 관리 품목",
        total=int(summary.get("total_product_count") or 0),
        height=270,
        inner_radius=76,
        outer_radius=116,
    )


def _build_outbound_frequency_chart(facts: dict[str, Any]) -> alt.Chart | None:
    summary = _inventory_status_summary(facts)
    counts = summary.get("frequency_counts") or {}
    rows = [
        {"label": grade, "count": int(counts.get(grade) or 0), "color": color}
        for grade, color in (
            ("A", "#2563eb"), ("B", "#0ea5e9"), ("C", "#14b8a6"),
            ("D", "#f59e0b"), ("E", "#f97316"), ("X", "#94a3b8"),
            ("빈도자료 부족", "#64748b"),
        )
    ]
    return _build_follow_up_chart(rows)


def _build_outbound_frequency_distribution_chart(facts: dict[str, Any]) -> alt.Chart | None:
    summary = _inventory_status_summary(facts)
    counts = summary.get("frequency_counts") or {}
    rows = [
        {"grade": grade, "count": int(counts.get(grade) or 0), "color": color}
        for grade, color in (
            ("A", "#2563eb"), ("B", "#0ea5e9"), ("C", "#14b8a6"),
            ("D", "#f59e0b"), ("E", "#f97316"), ("X", "#94a3b8"),
        )
    ]
    missing_count = int(counts.get("빈도자료 부족") or 0)
    if missing_count:
        rows.append({"grade": "빈도자료 부족", "count": missing_count, "color": "#64748b"})
    total = int(summary.get("total_product_count") or sum(row["count"] for row in rows))
    frame = pd.DataFrame(rows)
    if frame.empty:
        return None
    frame["pct"] = frame["count"].map(lambda value: (float(value) / total * 100.0) if total else 0.0)
    max_count = float(frame["count"].max() or 0.0)
    # Relative tracks reserve a small, uniform tail so the highest grade does
    # not visually merge into the background endpoint.
    frame["track"] = 102.0
    frame["relative_top_pct"] = frame["count"].map(lambda value: (float(value) / max_count * 100.0) if max_count else 0.0)
    frame["label_position"] = 113.0
    frame["label"] = frame.apply(lambda row: f"{int(row['count']):,}개 · {float(row['pct']):.1f}%", axis=1)
    order = frame["grade"].tolist()
    y = alt.Y("grade:N", sort=order, title=None, axis=alt.Axis(labelFontWeight=700, labelColor="#334155"))
    background = alt.Chart(frame).mark_bar(color="#e8edf4", cornerRadiusEnd=9, size=26).encode(
        x=alt.X("track:Q", scale=alt.Scale(domain=[0, 116]), axis=None), y=y
    )
    bars = alt.Chart(frame).mark_bar(cornerRadiusEnd=9, size=26).encode(
        x=alt.X("relative_top_pct:Q", scale=alt.Scale(domain=[0, 116]), axis=None),
        y=y,
        color=alt.Color("grade:N", legend=None, scale=alt.Scale(domain=order, range=frame["color"].tolist())),
        tooltip=[alt.Tooltip("grade:N", title="등급"), alt.Tooltip("count:Q", title="품목 수", format=",.0f"), alt.Tooltip("pct:Q", title="비율", format=".1f")],
    )
    labels = alt.Chart(frame).mark_text(align="right", color="#334155", fontSize=11, fontWeight=600).encode(
        x=alt.X("label_position:Q", scale=alt.Scale(domain=[0, 116]), axis=None), y=y, text="label:N"
    )
    return (background + bars + labels).properties(height=max(226, len(frame) * 37)).configure(background="transparent").configure_view(stroke=None)


def _render_inventory_cover_days(facts: dict[str, Any]) -> None:
    summary = ((facts.get("inventory") or {}).get("visual_phase2_summary") or {})
    rows = [
        {"label": "재고 없음", "count": int(summary.get("cover_zero_stock_count") or 0), "amount": 0.0},
        {"label": "잔여 기간 미만", "count": int(summary.get("cover_shortfall_count") or 0), "amount": 0.0},
        {"label": "잔여 기간 이상", "count": int(summary.get("cover_sufficient_count") or 0), "amount": 0.0},
        {"label": "수요 없음", "count": int(summary.get("cover_no_demand_count") or 0), "amount": 0.0},
        {"label": "자료 부족", "count": int(summary.get("cover_insufficient_count") or 0), "amount": 0.0},
    ]
    st.markdown("### 재고 커버일")
    chart = _build_count_donut(
        rows,
        total_label="재고커버 대상",
        total=int(summary.get("inventory_count") or 0),
        colors=["#dc2626", "#f59e0b", "#16a34a", "#94a3b8", "#64748b"],
    )
    if chart is not None:
        st.altair_chart(chart, width="stretch")
    else:
        st.caption("재고 커버일을 계산할 품목이 없습니다.")
    st.caption("재고 커버일은 현재고 ÷ 일평균 잔여 예상수요입니다.")


_INVENTORY_DETAIL_COLUMNS = (
    "재고상태", "위험 품목 여부", "위험 유형", "출고빈도등급", "3개월 출고발생수",
    "제품코드", "제품명", "규격", "제조사명", "제품그룹명", "제품구분명", "제품분류명",
    "주요매입처명", "현재재고수량", "평가월 예상수요", "재고수요비율", "재고 커버일",
    "위험사유", "위험보정잔여예상수요", "위험보정부족예상수량", "위험보정부족예상금액",
    "위험보정재고준비율", "재고커버 자료상태", "수요급증상위분류", "수요급증세부분류", "수요급증세부분류사유", "최근 정상 입고일",
    "입고 경과일", "정상 입고 거래일수", "평균 입고간격일", "입고 자료상태", "입고 지연후보",
)

_INVENTORY_DETAIL_SURGE_COLUMNS = (
    "수요급증상위분류",
    "수요급증세부분류",
    "수요급증세부분류사유",
)
_INVENTORY_DETAIL_INBOUND_HISTORY_COLUMNS = (
    "최근 정상 입고일",
    "입고 경과일",
    "정상 입고 거래일수",
    "평균 입고간격일",
)


def _inventory_detail_filter_values(
    *,
    inventory_status: str,
    frequency_grade: str,
    risk_filter: str,
    demand_surge_filter: str,
    vendor_key: str,
    search_text: str,
) -> dict[str, str]:
    """Keep editable widget values separate from the last submitted local query."""
    return {
        "inventory_status": str(inventory_status or "전체"),
        "frequency_grade": str(frequency_grade or "전체"),
        "risk_filter": str(risk_filter or "전체"),
        "demand_surge_filter": str(demand_surge_filter or "전체"),
        "vendor_key": str(vendor_key or "전체"),
        "search_text": str(search_text or "").strip(),
    }


def _integrated_inventory_detail_display_frame(frame: pd.DataFrame, display_limit: int) -> pd.DataFrame:
    """Apply only dashboard-specific blanks before the shared table formatter."""
    display = frame.head(max(0, int(display_limit))).copy()
    if display.empty:
        return display

    display = normalize_display_df_for_streamlit(display)

    surge_active = display.get("수요급증여부", pd.Series(False, index=display.index)).map(
        lambda value: value is True or str(value).strip().lower() in {"true", "1", "예"}
    )
    for column in _INVENTORY_DETAIL_SURGE_COLUMNS:
        if column in display.columns:
            display.loc[~surge_active, column] = ""

    latest_inbound = display.get("최근 정상 입고일", pd.Series("", index=display.index))
    no_inbound_history = latest_inbound.map(lambda value: not str(value or "").strip())
    for column in _INVENTORY_DETAIL_INBOUND_HISTORY_COLUMNS:
        if column in display.columns:
            display.loc[no_inbound_history, column] = pd.NA

    columns = [column for column in _INVENTORY_DETAIL_COLUMNS if column in display.columns]
    return display.loc[:, columns]


def _build_integrated_inventory_detail_frame(inventory: Mapping[str, Any]) -> pd.DataFrame:
    """Join exact risk-detail facts onto the complete inventory-status population."""
    base_rows = [dict(row) for row in (inventory.get("inventory_status_detail_rows") or []) if isinstance(row, Mapping)]
    frame = pd.DataFrame(base_rows)
    if frame.empty:
        return frame
    base_codes = [str(row.get("제품코드") or "").strip() for row in base_rows]
    if not all(base_codes) or len(base_codes) != len(set(base_codes)):
        raise ValueError("inventory_detail_product_code_not_unique")
    risk_by_product: dict[str, dict[str, Any]] = {}
    for row in (inventory.get("risk_detail_rows") or []):
        if not isinstance(row, Mapping):
            continue
        product_code = str(row.get("제품코드") or "").strip()
        if not product_code or product_code in risk_by_product:
            raise ValueError("risk_detail_product_code_not_unique")
        risk_by_product[product_code] = dict(row)
    merged_rows: list[dict[str, Any]] = []
    for base in base_rows:
        product_code = str(base.get("제품코드") or "").strip()
        risk = risk_by_product.get(product_code)
        merged = dict(base)
        merged["위험 품목 여부"] = "위험 품목" if risk is not None else "비위험 품목"
        merged["위험 유형"] = str((risk or {}).get("위험상태") or "")
        if risk is not None:
            for surge_column in ("수요급증여부", "수요급증상위분류", "수요급증세부분류"):
                base_value = merged.get(surge_column)
                risk_value = risk.get(surge_column)
                if base_value not in (None, "", False) and risk_value not in (None, "", False) and base_value != risk_value:
                    raise ValueError("inventory_risk_demand_surge_fact_mismatch")
            for column, value in risk.items():
                if column not in {
                    "제품코드", "제품명", "제조사명", "현재재고수량", "재고커버일",
                    "수요급증여부", "수요급증상위분류", "수요급증세부분류", "수요급증세부분류사유",
                }:
                    merged[column] = value
        merged_rows.append(merged)
    return pd.DataFrame(merged_rows)


def _filter_integrated_inventory_detail_rows(
    frame: pd.DataFrame,
    *,
    inventory_status: str,
    frequency_grade: str,
    risk_filter: str,
    vendor_key: str,
    search_text: str,
    demand_surge_filter: str = "전체",
) -> pd.DataFrame:
    filtered = frame.copy()
    if inventory_status != "전체":
        filtered = filtered.loc[filtered["재고상태"].eq(inventory_status)]
    if frequency_grade != "전체":
        filtered = filtered.loc[filtered["출고빈도등급"].eq(frequency_grade)]
    if risk_filter == "위험 품목":
        filtered = filtered.loc[filtered["위험 품목 여부"].eq("위험 품목")]
    elif risk_filter in {"긴급 부족", "부족 주의"}:
        filtered = filtered.loc[filtered["위험 유형"].eq(risk_filter)]
    elif risk_filter == "비위험 품목":
        filtered = filtered.loc[filtered["위험 품목 여부"].eq("비위험 품목")]
    surge_all = filtered.get("수요급증여부", pd.Series(False, index=filtered.index)).fillna(False).astype(bool)
    surge_top = filtered.get("수요급증상위분류", pd.Series("", index=filtered.index)).fillna("").astype(str)
    surge_detail = filtered.get("수요급증세부분류", pd.Series("", index=filtered.index)).fillna("").astype(str)
    if demand_surge_filter == "수요급증 전체":
        filtered = filtered.loc[surge_all]
    elif demand_surge_filter in {"기존 예상 초과", "예상외 출고 발생"}:
        filtered = filtered.loc[surge_top.eq(demand_surge_filter)]
    elif demand_surge_filter != "전체":
        filtered = filtered.loc[surge_detail.eq(demand_surge_filter)]
    if vendor_key and vendor_key != "전체":
        filtered = filtered.loc[filtered.get("_주요매입처필터키", pd.Series("", index=filtered.index)).eq(vendor_key)]
    token = str(search_text or "").strip().casefold()
    if token:
        filtered = filtered.loc[
            filtered["제품코드"].fillna("").astype(str).str.casefold().str.contains(token, regex=False)
            | filtered["제품명"].fillna("").astype(str).str.casefold().str.contains(token, regex=False)
            | filtered["제조사명"].fillna("").astype(str).str.casefold().str.contains(token, regex=False)
        ]
    filtered["재고수요비율"] = pd.to_numeric(
        filtered.get("재고수요비율", pd.Series(index=filtered.index, dtype="float64")), errors="coerce"
    )
    filtered["위험보정부족예상금액"] = pd.to_numeric(
        filtered.get("위험보정부족예상금액", pd.Series(index=filtered.index, dtype="float64")), errors="coerce"
    ).fillna(0)
    filtered["_위험정렬"] = filtered["위험 품목 여부"].ne("위험 품목").astype(int)
    return filtered.sort_values(
        ["_위험정렬", "위험보정부족예상금액", "재고상태", "재고수요비율", "제품코드"],
        ascending=[True, False, True, True, True],
        kind="stable",
        na_position="last",
    ).drop(columns=["_위험정렬"], errors="ignore")


def _render_inventory_status_detail(facts: dict[str, Any], cache: Mapping[str, Any], *, render_mode: str) -> None:
    inventory = facts.get("inventory") or {}
    summary = _inventory_status_summary(facts)
    st.markdown("### 재고 현황 상세")
    if render_mode != "primary":
        st.caption("통합 상세표와 Excel은 현재 Dashboard 조회 세션에서만 사용할 수 있습니다.")
        return
    try:
        frame = _build_integrated_inventory_detail_frame(inventory)
    except ValueError as exc:
        log.warning("[dashboard.inventory_detail] status=fail_closed reason_code=%s", str(exc))
        st.warning("통합 상세의 제품 연결 상태가 일치하지 않아 이번 상세를 표시하지 않습니다.")
        return
    if frame.empty:
        st.caption("표시할 재고 현황 품목이 없습니다.")
        return
    namespace = _dashboard_render_namespace(dict(cache), render_mode="inventory-status-detail")
    risk_rows = [dict(row) for row in (inventory.get("risk_detail_rows") or []) if isinstance(row, Mapping)]
    vendor_options, vendor_labels = _risk_detail_vendor_options(risk_rows)
    status_options = ["전체", "긴급 부족", "재고 경고", "적정 재고", "과다 재고", "예상수요 없음", "자료 부족"]
    frequency_options = ["전체", "A", "B", "C", "D", "E", "X", "빈도자료 부족"]
    applied_key = f"__dashboard_lite_inventory_detail_applied::{namespace}"
    with st.form(key=f"dashboard_inventory_detail_form::{namespace}", clear_on_submit=False, enter_to_submit=False):
        filter_cols = st.columns((19, 17, 18, 23))
        with filter_cols[0]:
            selected_status = st.selectbox("재고 상태", status_options, key=f"dashboard_inventory_detail::{namespace}::state")
        with filter_cols[1]:
            selected_frequency = st.selectbox("출고빈도 등급", frequency_options, key=f"dashboard_inventory_detail::{namespace}::frequency")
        with filter_cols[2]:
            selected_risk = st.selectbox("위험 품목", ["전체", "위험 품목", "긴급 부족", "부족 주의", "비위험 품목"], key=f"dashboard_inventory_detail::{namespace}::risk")
        with filter_cols[3]:
            selected_surge = st.selectbox(
                "수요급증 유형",
                ["전체", "수요급증 전체", "기존 예상 초과", "예상외 출고 발생", "신규 출고 후보", "계절성 재발생 후보", "3개월 이상 재출고", "예상 누락", "분류자료 부족"],
                key=f"dashboard_inventory_detail::{namespace}::surge",
            )
        lower_filter_cols = st.columns((10, 9, 2.5), gap="small", vertical_alignment="bottom")
        with lower_filter_cols[0]:
            vendor_key = st.selectbox("주요매입처", vendor_options, format_func=lambda value: vendor_labels.get(value, "매입처 미확인"), key=f"dashboard_inventory_detail::{namespace}::vendor")
        with lower_filter_cols[1]:
            search_text = st.text_input("제품 검색", key=f"dashboard_inventory_detail::{namespace}::search")
        with lower_filter_cols[2]:
            submitted = st.form_submit_button("재고 상세 조회", type="primary", width="stretch")
    if submitted:
        st.session_state[applied_key] = _inventory_detail_filter_values(
            inventory_status=selected_status,
            frequency_grade=selected_frequency,
            risk_filter=selected_risk,
            demand_surge_filter=selected_surge,
            vendor_key=vendor_key,
            search_text=search_text,
        )
    applied = st.session_state.get(applied_key)
    if not isinstance(applied, Mapping):
        st.info("조건을 선택한 후 [재고 상세 조회]를 눌러주세요.")
        return
    filtered = _filter_integrated_inventory_detail_rows(
        frame,
        inventory_status=str(applied.get("inventory_status") or "전체"),
        frequency_grade=str(applied.get("frequency_grade") or "전체"),
        risk_filter=str(applied.get("risk_filter") or "전체"),
        demand_surge_filter=str(applied.get("demand_surge_filter") or "전체"),
        vendor_key=str(applied.get("vendor_key") or "전체"),
        search_text=str(applied.get("search_text") or ""),
    )
    applied_status = str(applied.get("inventory_status") or "전체")
    applied_frequency = str(applied.get("frequency_grade") or "전체")
    applied_risk = str(applied.get("risk_filter") or "전체")
    applied_surge = str(applied.get("demand_surge_filter") or "전체")
    applied_vendor = str(applied.get("vendor_key") or "전체")
    applied_search = str(applied.get("search_text") or "")
    display_limit = min(300, len(filtered))
    st.caption(
        f"관리 품목 {int(summary.get('total_product_count') or 0):,}개 / "
        f"예상수요 품목 {int(summary.get('expected_demand_product_count') or 0):,}개 / "
        f"위험 품목 {len(risk_rows):,}개 / 필터 결과 {len(filtered):,}개"
    )
    if filtered.empty:
        st.info("선택한 조건에 해당하는 품목이 없습니다.")
        return
    display = _integrated_inventory_detail_display_frame(filtered, display_limit)
    display, common_column_config, _table_width, _table_height = build_sims_table_display_config(
        display,
        action_name="Dashboard 재고 현황 상세",
        add_row_no=False,
        enable_pinning=False,
        min_height=170,
        max_height=520,
    )
    risk_text_column_config = {
        column: config
        for column, config in _risk_detail_display_column_config().items()
        if not is_sims_numeric_display_col(display, column)
    }
    st.dataframe(
        display,
        width="stretch",
        height=_risk_detail_display_height(display_limit),
        hide_index=True,
        column_config={
            **common_column_config,
            **risk_text_column_config,
            "재고상태": st.column_config.TextColumn("재고 상태", width=110, pinned=True, alignment="center"),
            "위험 품목 여부": st.column_config.TextColumn("위험 품목", width=100, pinned=True, alignment="center"),
            "위험 유형": st.column_config.TextColumn("위험 유형", width=100, pinned=True, alignment="center"),
            "출고빈도등급": st.column_config.TextColumn("출고빈도", width=90, alignment="center"),
        },
    )
    if not bool(require_permission("EXPORT_EXCEL", show_error=False)):
        st.warning("다운로드 권한이 없습니다. 필요 권한: EXPORT_EXCEL (엑셀/CSV 다운로드)")
        return
    filter_signature = hashlib.sha256(
        "|".join((applied_status, applied_frequency, applied_risk, applied_surge, applied_vendor, applied_search)).encode("utf-8")
    ).hexdigest()[:12]
    excel_key = f"__dashboard_lite_inventory_detail_excel::{namespace}::{filter_signature}"
    cache_entry = st.session_state.get(excel_key)
    if not isinstance(cache_entry, dict) or not isinstance(cache_entry.get("bytes"), (bytes, bytearray)):
        if st.button("현재 필터 결과 Excel 준비", key=f"__dashboard_lite_inventory_detail_prepare::{namespace}::{filter_signature}", width="stretch"):
            conditions = _risk_detail_query_conditions(
                dict(cache), facts, excel_created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            conditions.extend([
                {"조건명": "재고 상태", "값": applied_status},
                {"조건명": "출고빈도 등급", "값": applied_frequency},
                {"조건명": "위험 품목", "값": applied_risk},
                {"조건명": "수요급증 유형", "값": applied_surge},
                {"조건명": "주요매입처", "값": vendor_labels.get(applied_vendor, applied_vendor)},
                {"조건명": "제품 검색", "값": applied_search},
            ])
            try:
                bytes_value, export_info = build_dashboard_inventory_detail_excel_bytes(filtered, conditions)
                st.session_state[excel_key] = {"bytes": bytes_value, "export_info": export_info, "row_count": len(filtered)}
                st.rerun()
            except Exception as exc:
                log.warning("[dashboard.inventory_detail_export] export_rows=%s sheet_count=0 bytes_size=0 permission_allowed=True elapsed_ms=0 success=False error_type=%s", len(filtered), type(exc).__name__)
                st.warning("현재 필터 결과 Excel을 준비하지 못했습니다.")
        return
    st.download_button(
        "현재 필터 결과 Excel 다운로드",
        data=cache_entry["bytes"],
        file_name="dashboard_inventory_detail.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"__dashboard_lite_inventory_detail_download::{namespace}::{filter_signature}",
        width="stretch",
    )


def _visible_purchase_trend_rows(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Limit the purchase chart without trimming internal history facts."""
    inventory = facts.get("inventory") or {}
    period = facts.get("period") or {}
    month_from = str(period.get("month_from") or "").strip()
    evaluation_month = str(period.get("evaluation_month") or "").strip()
    rows = [dict(row) for row in (inventory.get("purchase_trend_rows") or []) if isinstance(row, Mapping)]
    if len(month_from) != 6 or len(evaluation_month) != 6:
        return rows[:18]
    return [row for row in rows if month_from <= str(row.get("month") or "") <= evaluation_month][:18]


def _vendor_supply_scope_caption(summary: Mapping[str, Any]) -> str:
    total = int(summary.get("status_risk_rows") or 0)
    positive = int(summary.get("amount_positive_risk_rows") or summary.get("risk_rows") or 0)
    zero = int(summary.get("amount_zero_risk_rows") or max(0, total - positive))
    return (
        f"공급 연결은 금액 양수 위험 {positive:,}개를 기준으로 집계합니다. "
        f"전체 위험 {total:,}개 중 금액 0 위험 {zero:,}개는 공급별 금액 집계에서 제외합니다."
    )


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


def _build_demand_surge_summary_type_chart(rows: list[dict[str, Any]]) -> alt.Chart | None:
    """Use the persisted surge partition, keeping the operator-facing order stable."""
    preferred_order = ["신규 출고 후보", "계절성 재발생 후보", "3개월 이상 재출고", "예상 누락", "분류자료부족"]
    frame = pd.DataFrame([dict(row) for row in rows if int(row.get("count") or 0) > 0])
    if frame.empty:
        return None
    frame["order"] = frame["label"].map({label: index for index, label in enumerate(preferred_order)})
    frame = frame.sort_values(["order", "label"], kind="stable")
    frame["count_label"] = frame["count"].map(lambda value: f"{int(value):,}개")
    colors = {
        "신규 출고 후보": "#7c3aed",
        "계절성 재발생 후보": "#0f766e",
        "3개월 이상 재출고": "#2563eb",
        "예상 누락": "#64748b",
        "분류자료부족": "#94a3b8",
    }
    order = frame["label"].tolist()
    y = alt.Y("label:N", title=None, sort=order, axis=alt.Axis(labelLimit=165, labelColor="#475569"))
    bars = alt.Chart(frame).mark_bar(cornerRadiusEnd=5, size=18).encode(
        x=alt.X("count:Q", title=None, axis=alt.Axis(tickMinStep=1, labelColor="#64748b")),
        y=y,
        color=alt.Color("label:N", legend=None, scale=alt.Scale(domain=list(colors), range=list(colors.values()))),
        tooltip=[alt.Tooltip("label:N", title="유형"), alt.Tooltip("count:Q", title="품목 수", format=",.0f")],
    )
    labels = alt.Chart(frame).mark_text(align="left", dx=5, color="#334155", fontSize=11, fontWeight=600).encode(
        x="count:Q", y=y, text="count_label:N"
    )
    return (bars + labels).properties(height=max(164, min(208, len(frame) * 38))).configure(background="transparent").configure_view(stroke=None)


def _build_demand_surge_summary_donut(state: Mapping[str, Any]) -> alt.Chart | None:
    total = int(state.get("total") or 0)
    if total <= 0 or not bool(state.get("top_partition_valid")):
        return None
    rows = [
        {"label": row["label"], "count": int(row["count"]), "color": color, "text_color": "#ffffff"}
        for row, color in zip(state.get("top_rows") or [], ("#f97316", "#7c3aed"))
    ]
    return _build_labeled_summary_donut(
        rows,
        total_label="수요급증 품목",
        total=total,
        height=226,
        # This card has its legend below the chart. Keep the status donut's
        # larger shared geometry intact, but reserve vertical label headroom
        # here for both a 100% arc and a small top slice.
        inner_radius=55,
        outer_radius=94,
        top_padding=18,
    )


def _demand_surge_flow_markup(unexpected_total: int) -> str:
    """Return the relation arrow from the unexpected-outbound slice to its breakdown."""
    if unexpected_total <= 0:
        return ""
    return (
        '<div class="dashboard-lite-demand-surge-flow" aria-label="예상외 출고 발생 세부 유형 연결">'
        f'<span class="dashboard-lite-demand-surge-flow-label">예상외 출고 {unexpected_total:,}개</span>'
        '<svg class="dashboard-lite-demand-surge-flow-arrow" viewBox="0 0 180 16" preserveAspectRatio="none" aria-hidden="true">'
        '<defs><linearGradient id="dashboard-demand-flow" x1="0" x2="1"><stop offset="0" stop-color="#a78bfa" stop-opacity=".42"/>'
        '<stop offset=".68" stop-color="#7c3aed" stop-opacity=".88"/><stop offset="1" stop-color="#6d28d9"/></linearGradient></defs>'
        '<path d="M1 3 L145 3 L179 8 L145 13 L1 13 Z" fill="url(#dashboard-demand-flow)"/>'
        '</svg></div>'
    )


def _render_demand_surge_summary_card(facts: dict[str, Any], *, render_namespace: str) -> None:
    """Render one local-only card from demand-surge facts already built for Dashboard."""
    state = _demand_surge_presentation_state(facts)
    total = int(state["total"])
    _render_summary_card_heading("trend", "surge", "수요급증 세부", "기존 예상 초과와 예상외 출고 발생을 구분합니다.")
    if total <= 0:
        st.caption("수요급증 품목이 없습니다.")
        return
    with st.container(key=f"dashboard_inventory_card_body__surge__{render_namespace}"):
        composition_col, type_col = st.columns([42, 58], gap="small", vertical_alignment="center")
        with composition_col:
            if state["top_partition_valid"]:
                rows = [
                    {"label": row["label"], "count": int(row["count"]), "color": color, "text_color": "#ffffff"}
                    for row, color in zip(state["top_rows"], ("#f97316", "#7c3aed"))
                ]
                with st.container():
                    chart = _build_demand_surge_summary_donut(state)
                    if chart is not None:
                        st.altair_chart(chart, width="stretch")
                    _render_inventory_summary_legend(rows, total=total)
            else:
                st.caption("상위 분류 합계가 전체 수요급증과 일치하지 않아 독립 지표로 표시합니다.")
        with type_col:
            if state["detail_partition_valid"]:
                with st.container():
                    st.markdown(_demand_surge_flow_markup(int(state.get("unexpected_total") or 0)), unsafe_allow_html=True)
                    st.markdown('<div class="dashboard-lite-demand-surge-detail-title">예상외 출고 발생 세부 유형</div>', unsafe_allow_html=True)
                    chart = _build_demand_surge_summary_type_chart(state["detail_rows"])
                    if chart is not None:
                        st.altair_chart(chart, width="stretch")
                    else:
                        st.caption("예상외 출고 발생 품목이 없습니다.")
            else:
                st.caption("하위 유형 합계가 예상외 출고 발생과 일치하지 않아 독립 지표로 표시합니다.")
    st.markdown('<div class="dashboard-lite-inventory-card-footer">세부 유형은 기존 수요급증 분류 계약을 사용합니다.</div>', unsafe_allow_html=True)


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


def _build_stock_cover_donut(rows: list[dict[str, Any]], *, total: int) -> alt.Chart | None:
    frame = pd.DataFrame([row for row in rows if int(row.get("count") or 0) > 0])
    if frame.empty:
        return None
    colors = ["#dc2626", "#f59e0b", "#16a34a", "#2563eb", "#64748b"]
    labels = [row["label"] for row in rows]
    donut = alt.Chart(frame).mark_arc(innerRadius=54, outerRadius=82).encode(
        theta=alt.Theta("count:Q"),
        color=alt.Color("label:N", legend=alt.Legend(orient="bottom", title=None), scale=alt.Scale(domain=labels, range=colors)),
        tooltip=[alt.Tooltip("label:N", title="상태"), alt.Tooltip("count:Q", title="품목 수", format=",.0f")],
    )
    center_label = alt.Chart(pd.DataFrame({"label": ["재고커버"], "value": [f"총 {total:,}개"]})).mark_text(
        dy=-8, fontSize=14, fontWeight="bold", color="#334155"
    ).encode(text="label:N")
    center_total = alt.Chart(pd.DataFrame({"value": [f"총 {total:,}개"]})).mark_text(
        dy=12, fontSize=13, color="#475569"
    ).encode(text="value:N")
    return (donut + center_label + center_total).properties(height=245)


def _build_follow_up_chart(rows: list[dict[str, Any]]) -> alt.Chart | None:
    frame = pd.DataFrame([row for row in rows if int(row.get("count") or 0) > 0])
    if frame.empty:
        return None
    frame = frame.sort_values(["count", "label"], ascending=[False, True], kind="stable")
    frame["count_label"] = frame["count"].map(lambda value: f"{int(value):,}개")
    order = frame["label"].tolist()
    y = alt.Y("label:N", title=None, sort=order, axis=alt.Axis(labelLimit=180))
    bars = alt.Chart(frame).mark_bar(cornerRadiusEnd=3).encode(
        x=alt.X("count:Q", title="품목 수", axis=alt.Axis(tickMinStep=1)),
        y=y,
        color=alt.Color("label:N", legend=None, scale=alt.Scale(domain=order, range=frame["color"].tolist())),
        tooltip=[alt.Tooltip("label:N", title="확인 항목"), alt.Tooltip("count:Q", title="품목 수", format=",.0f")],
    )
    labels = alt.Chart(frame).mark_text(align="left", dx=5, color="#334155").encode(
        x=alt.X("count:Q"), y=y, text="count_label:N"
    )
    return (bars + labels).properties(height=max(150, min(270, len(frame) * 38)))


def _render_visual_phase2(facts: dict[str, Any]) -> None:
    inventory = facts.get("inventory") or {}
    summary = inventory.get("visual_phase2_summary") or {}
    if not summary:
        return
    total = int(summary.get("inventory_count") or 0)
    cover_rows = [
        {"label": "재고 없음", "count": int(summary.get("cover_zero_stock_count") or 0), "amount": 0.0},
        {"label": "잔여 기간 미만", "count": int(summary.get("cover_shortfall_count") or 0), "amount": 0.0},
        {"label": "잔여 기간 이상", "count": int(summary.get("cover_sufficient_count") or 0), "amount": 0.0},
        {"label": "수요 없음", "count": int(summary.get("cover_no_demand_count") or 0), "amount": 0.0},
        {"label": "자료 부족", "count": int(summary.get("cover_insufficient_count") or 0), "amount": 0.0},
    ]
    follow_up_rows = [
        {"label": "입고 지연 후보", "count": int(summary.get("inbound_delay_candidate_count") or 0), "color": "#f59e0b"},
        {"label": "과잉 후보", "count": int(summary.get("overstock_candidate_count") or 0), "color": "#7c3aed"},
        {"label": "최근 매입 없음", "count": int(summary.get("recent_purchase_none_count") or 0), "color": "#94a3b8"},
        {"label": "매입처 미확인", "count": int(summary.get("vendor_unknown_count") or 0), "color": "#94a3b8"},
        {"label": "수요급증", "count": int(summary.get("demand_surge_count") or 0), "color": "#0f766e"},
        {"label": "재고 없음", "count": int(summary.get("cover_zero_stock_count") or 0), "color": "#dc2626"},
    ]
    cover_col, follow_up_col = st.columns([35, 65])
    with cover_col:
        st.markdown("### 재고커버 구성")
        if bool(summary.get("cover_partition_valid")):
            donut = _build_count_donut(
                cover_rows,
                total_label="재고커버 총",
                total=total,
                colors=["#dc2626", "#f59e0b", "#16a34a", "#94a3b8", "#64748b"],
            )
            if donut is not None:
                st.altair_chart(donut, width="stretch")
            else:
                st.caption("재고커버 판정 대상이 없습니다.")
        else:
            st.caption("재고커버 상태 집계가 불완전해 도넛으로 표시하지 않았습니다.")
        st.caption("재고 없음과 잔여 기간 미만을 우선 확인합니다.")
    with follow_up_col:
        st.markdown("### 후속 확인 항목")
        chart = _build_follow_up_chart(follow_up_rows)
        if chart is not None:
            st.altair_chart(chart, width="stretch")
        else:
            st.caption("현재 후속 확인 후보가 없습니다.")
        st.caption("항목은 서로 중복될 수 있으며, 비율 합계로 해석하지 않습니다.")

    purchase_rows = _visible_purchase_trend_rows(facts)
    if summary.get("purchase_trend_status") == "ready" and purchase_rows:
        purchase = pd.DataFrame(purchase_rows)
        if {"month", "amount"}.issubset(purchase.columns):
            amount_unit = _facts_amount_display_unit(facts)
            total_amount = float(pd.to_numeric(purchase["amount"], errors="coerce").fillna(0.0).abs().sum())
            divisor, unit_label = _amount_display_spec(amount_unit, total_amount)
            purchase = purchase.copy()
            purchase["display_amount"] = pd.to_numeric(purchase["amount"], errors="coerce").fillna(0.0) / divisor
            purchase = purchase.sort_values("month", kind="stable")
            st.markdown("### 월별 매입 추세")
            chart = alt.Chart(purchase).mark_bar(color="#0f766e", cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
                x=alt.X("month:N", title="월", sort=purchase["month"].tolist()),
                y=alt.Y("display_amount:Q", title=f"매입금액 ({unit_label})", axis=alt.Axis(format=",.0f")),
                tooltip=[alt.Tooltip("month:N", title="기준월"), alt.Tooltip("amount:Q", title="매입금액", format=",.0f")],
            ).properties(height=230)
            st.altair_chart(chart, width="stretch")
            st.caption("공통 매출 원천에서 이미 확보한 월별 매입금액을 재사용합니다.")

    briefing_lines = [str(line) for line in (summary.get("briefing_lines") or []) if str(line).strip()][:5]
    if briefing_lines:
        with st.container(border=True):
            st.markdown("### 오늘의 재고·공급 브리핑")
            for line in briefing_lines:
                st.caption(line)


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
    basis_days = int(summary.get("basis_days") or _DASHBOARD_PROFILE_SCALAR_DEFAULTS["major_purchase_vendor_days"])
    basis_cutoff_date = str(summary.get("basis_cutoff_date") or "").strip()

    st.markdown("### 매입처별 부족예상 TOP 10")
    st.caption(
        f"최근 {basis_days}일 정상 입고수량이 가장 큰 실제 매입처별 위험금액입니다. "
        f"기간 내 입고가 없을 때만 제품마스터 발주처를 사용합니다."
        + (f" 판단 기준일 {basis_cutoff_date}." if basis_cutoff_date else "")
    )
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
        st.caption(_vendor_supply_scope_caption(summary))

    if not rows:
        with chart_col:
            st.caption("정상 귀속된 긴급 부족·부족 주의 매입처가 없습니다.")
        return
    frame = pd.DataFrame(rows).head(10).copy()
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
    ).properties(height=max(180, min(420, len(order) * 34)))
    with chart_col:
        st.altair_chart(chart, width="stretch")
        st.caption(f"주요 매입처 판정기간은 최근 {basis_days}일이며 정상 입고수량을 기준으로 선정합니다.")


def _render_vendor_stock_risk_summary_card(facts: dict[str, Any], *, render_namespace: str) -> None:
    """Render the existing vendor-risk TOP rows without promoting supply-link diagnostics."""
    inventory = facts.get("inventory") or {}
    summary = inventory.get("vendor_stock_risk_summary") or {}
    rows = inventory.get("vendor_stock_risk_top_rows") or []
    _render_summary_card_heading("truck", "vendor", "매입처별 부족예상 TOP 10", "대표 매입처별 위험보정 부족금액입니다.")
    if not summary or not rows:
        st.caption("정상 귀속된 긴급 부족·부족 주의 매입처가 없습니다.")
        return
    amount_unit = _facts_amount_display_unit(facts)
    total_amount = float(summary.get("total_adjusted_shortage_amount") or 0)
    if amount_unit == "auto":
        divisor, _ = _amount_display_spec("auto", abs(total_amount))
        amount_unit = "million" if divisor == 1_000_000 else ("thousand" if divisor == 1_000 else "won")
    divisor, unit_label = _amount_display_spec(amount_unit, total_amount)
    frame = pd.DataFrame(rows).head(10).copy()
    frame["표시매입처"] = frame.apply(
        lambda row: str(row.get("주요매입처명") or "").strip()
        if str(row.get("주요매입처명") or "").strip() and frame["주요매입처명"].astype(str).eq(str(row.get("주요매입처명") or "")).sum() == 1
        else f"{str(row.get('주요매입처명') or '').strip() or '미확인'} [{str(row.get('주요매입처코드') or '').strip()}]",
        axis=1,
    )
    frame["긴급부족금액"] = pd.to_numeric(
        frame["긴급부족금액"] if "긴급부족금액" in frame else pd.Series(0.0, index=frame.index), errors="coerce"
    ).fillna(0.0)
    frame["부족주의금액"] = pd.to_numeric(
        frame["부족주의금액"] if "부족주의금액" in frame else pd.Series(0.0, index=frame.index), errors="coerce"
    ).fillna(0.0)
    frame["표시금액"] = (frame["긴급부족금액"] + frame["부족주의금액"]) / divisor
    frame["비율"] = (frame["긴급부족금액"] + frame["부족주의금액"]).map(
        lambda value: (float(value) / total_amount * 100.0) if total_amount else 0.0
    )
    frame["금액비율"] = frame.apply(
        lambda row: f"{_fmt_number(float(row['표시금액']), 1 if divisor > 1 else 0)} {unit_label} · {float(row['비율']):.1f}%",
        axis=1,
    )
    with st.container(key=f"dashboard_inventory_card_body__vendor__{render_namespace}"):
        chart = _build_vendor_stock_risk_summary_chart(frame, unit_label=unit_label)
        if chart is not None:
            st.altair_chart(chart, width="stretch")
    basis_days = int(summary.get("basis_days") or _DASHBOARD_PROFILE_SCALAR_DEFAULTS["major_purchase_vendor_days"])
    st.markdown(
        f'<div class="dashboard-lite-inventory-card-footer">최근 {basis_days}일 정상 입고수량 최대 매입처를 사용합니다. '
        f'{html.escape(_vendor_supply_scope_caption(summary))}</div>',
        unsafe_allow_html=True,
    )


def _build_vendor_stock_risk_summary_chart(frame: pd.DataFrame, *, unit_label: str) -> alt.Chart | None:
    """Use relative tracks for the existing TOP rows without changing their rank or amount."""
    if frame.empty:
        return None
    chart_frame = frame.copy()
    max_amount = float(chart_frame["표시금액"].max() or 0.0)
    # Use the same visual headroom contract as the frequency chart.  Amounts
    # and rank remain untouched; only the drawing scale reserves the tail.
    chart_frame["track"] = 104.0
    chart_frame["relative_top_pct"] = chart_frame["표시금액"].map(
        lambda value: (float(value) / max_amount * 100.0) if max_amount else 0.0
    )
    chart_frame["label_position"] = 118.0
    order = chart_frame["표시매입처"].tolist()
    y = alt.Y("표시매입처:N", sort=order, title=None, axis=alt.Axis(labelLimit=145, labelColor="#475569"))
    background = alt.Chart(chart_frame).mark_bar(cornerRadiusEnd=5, color="#e8edf4", size=19).encode(
        x=alt.X("track:Q", scale=alt.Scale(domain=[0, 122]), axis=None), y=y
    )
    bars = alt.Chart(chart_frame).mark_bar(cornerRadiusEnd=5, color="#dc2626", size=19).encode(
        x=alt.X("relative_top_pct:Q", scale=alt.Scale(domain=[0, 122]), axis=None),
        y=y,
        tooltip=[
            alt.Tooltip("표시매입처:N", title="주요 매입처"),
            alt.Tooltip("주요매입처명:N", title="전체 매입처명"),
            alt.Tooltip("표시금액:Q", title=f"부족예상금액 ({unit_label})", format=",.1f"),
            alt.Tooltip("긴급부족금액:Q", title="긴급 부족금액", format=",.0f"),
            alt.Tooltip("부족주의금액:Q", title="부족 주의 금액", format=",.0f"),
            alt.Tooltip("비율:Q", title="전체 부족금액 대비", format=".1f"),
        ],
    )
    labels = alt.Chart(chart_frame).mark_text(align="right", color="#334155", fontSize=10, fontWeight=600).encode(
        x=alt.X("label_position:Q", scale=alt.Scale(domain=[0, 122]), axis=None), y=y, text="금액비율:N"
    )
    return (background + bars + labels).properties(height=max(236, min(266, len(order) * 25))).configure(background="transparent").configure_view(stroke=None)


def _render_demand_surge_detail_summary(facts: dict[str, Any]) -> None:
    """Render the persisted demand-surge partitions without querying new rows."""
    summary = (facts.get("inventory") or {}).get("stock_demand_surge_summary") or {}
    state = _demand_surge_presentation_state(facts)
    total = int(state["total"])
    if total <= 0:
        return

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


def _transaction_cycle_presentation(turnover: dict[str, Any], *, side: str) -> dict[str, Any]:
    """Return a small, snapshot-safe presentation model for one transaction side."""
    is_purchase = side == "purchase"
    label = "매입" if is_purchase else "매출"
    raw_status = str(turnover.get(f"{side}_data_status") or "").strip().lower()
    status = {
        "normal": "ready",
        "missing": "no_data",
        "success": "ready",
        "single_trade_day": "insufficient_days",
    }.get(raw_status, raw_status)
    if status == "source_required":
        return {"status": status, "message": f"일자 단위 정상 {label} 거래일 자료 연결 필요"}
    if status == "no_data":
        return {"status": status, "message": f"최근 90일 정상 {label} 거래가 없습니다."}
    if status == "error":
        return {"status": status, "message": f"최근 정상 {label} 거래일을 계산하지 못했습니다."}

    latest = str(turnover.get(f"{side}_latest_date") or "").strip()
    if len(latest) == 8 and latest.isdigit():
        latest = f"{latest[:4]}-{latest[4:6]}-{latest[6:]}"
    unique_days = turnover.get(f"{side}_unique_trade_days")
    interval = turnover.get(f"{side}_average_interval_days")
    return {
        "status": status or "no_data",
        "message": "",
        "latest_date": latest or "-",
        "elapsed_days": turnover.get(f"{side}_elapsed_days"),
        "unique_trade_days": unique_days,
        "average_interval_days": interval,
        "average_label": "거래일 부족" if status == "insufficient_days" else None,
    }


def _render_turnover(facts: dict[str, Any]) -> None:
    turnover = facts.get("transaction_cycle") or facts.get("turnover_days") or {}

    def _render_side(side: str) -> None:
        label = "매입" if side == "purchase" else "매출"
        presentation = _transaction_cycle_presentation(turnover, side=side)
        st.markdown(f"#### {label} 거래 주기")
        st.caption(f"최근 정상 {label} {period_days}일 기준")
        if presentation.get("message"):
            st.caption(str(presentation["message"]))
            return
        elapsed = presentation.get("elapsed_days")
        unique_days = presentation.get("unique_trade_days")
        interval = presentation.get("average_interval_days")
        st.markdown(f"최근 정상 거래일: **{presentation.get('latest_date') or '-'}**")
        st.markdown(f"경과일: **{'-' if elapsed in (None, '') else f'{elapsed}일'}**")
        st.markdown(f"고유 거래일: **{'-' if unique_days in (None, '') else f'{unique_days}일'}**")
        average_label = presentation.get("average_label")
        average_value = average_label or ("-" if interval in (None, "") else f"{_fmt_number(interval)}일")
        st.markdown(f"평균 거래간격: **{average_value}**")

    period_days = int(turnover.get("period_days") or 90)
    purchase_col, sales_col = st.columns(2)
    with purchase_col:
        _render_side("purchase")
    with sales_col:
        _render_side("sales")
    st.caption("본 지표는 ERP의 정상 매입·매출 거래일을 기준으로 계산합니다.")


def _dashboard_action_detail_pair(
    facts: dict[str, Any],
    *,
    action_id: Any,
    product_code: Any,
) -> dict[str, Any] | None:
    """Return the exact current action only when its cached detail row exists."""
    wanted_action_id = str(action_id or "").strip()
    wanted_product_code = str(product_code or "").strip()
    if not wanted_action_id or not wanted_product_code:
        return None
    matched_action = next(
        (
            action
            for action in facts.get("today_actions") or []
            if isinstance(action, dict)
            and str(action.get("action_id") or "").strip() == wanted_action_id
            and str(action.get("target_code") or action.get("product_code") or "").strip() == wanted_product_code
        ),
        None,
    )
    if matched_action is None:
        return None
    detail_rows = ((facts.get("inventory") or {}).get("risk_detail_rows") or [])
    if not any(
        isinstance(row, dict) and str(row.get("제품코드") or "").strip() == wanted_product_code
        for row in detail_rows
    ):
        return None
    return matched_action


def _dashboard_action_detail_selection(
    cache: dict[str, Any],
    action: dict[str, Any],
    *,
    facts: dict[str, Any],
) -> dict[str, str] | None:
    """Build a local-only selection for the active Dashboard cache."""
    room_id = get_current_chat_room_id()
    company_id = str(cache.get("company_id") or "").strip()
    event_id = str(cache.get("dashboard_event_id") or "").strip()
    action_id = str(action.get("action_id") or "").strip()
    product_code = str(action.get("target_code") or action.get("product_code") or "").strip()
    if not all((room_id, company_id, event_id, action_id, product_code)):
        return None
    if _dashboard_action_detail_pair(facts, action_id=action_id, product_code=product_code) is None:
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
    _request_dashboard_scroll_suppression("action_detail")
    log.info(
        "[dashboard.action_detail] stage=callback_selected room_match=True company_match=True event_match=True "
        "action_id_present=True product_code_present=True detail_match_count=0 db_query_count=0 chat_push_count=0 suppress_autoscroll=True risk_filter_mutated=False"
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


def _today_actions_presentation_state(actions: Any) -> dict[str, Any]:
    """Summarize only the existing TOP action rows for Dashboard display."""
    rows = [dict(action) for action in (actions or [])[:10] if isinstance(action, dict)]
    status_rows: list[dict[str, Any]] = []
    status_index: dict[str, dict[str, Any]] = {}
    type_rows: list[dict[str, Any]] = []
    type_index: dict[str, dict[str, Any]] = {}
    type_labels = {
        "stock_shortage": "재고 부족",
        "sales_decline": "매출 감소",
        "inbound_delay": "입고 지연",
        "overstock_candidate": "과잉 후보",
    }
    for fallback_index, action in enumerate(rows, start=1):
        status = _legacy_action_status(action)
        status_item = status_index.get(status)
        if status_item is None:
            status_item = {"label": status, "count": 0, "amount": 0.0, "amount_available": True}
            status_index[status] = status_item
            status_rows.append(status_item)
        status_item["count"] += 1
        if str(action.get("evidence_unit") or "") == "원" and action.get("evidence_value") not in (None, ""):
            status_item["amount"] += float(action.get("evidence_value") or 0)
        else:
            status_item["amount_available"] = False

        cause_type = str(action.get("cause_type") or "").strip()
        label = type_labels.get(cause_type, cause_type or "기타")
        type_item = type_index.get(label)
        if type_item is None:
            type_item = {"label": label, "count": 0}
            type_index[label] = type_item
            type_rows.append(type_item)
        type_item["count"] += 1

    status_order = {"긴급 부족": 0, "부족 주의": 1, "매출 감소": 2, "입고 지연": 3, "과잉 후보": 4}
    status_rows.sort(key=lambda row: (status_order.get(str(row["label"]), 99), str(row["label"])))
    type_rows.sort(key=lambda row: (-int(row["count"]), str(row["label"])))
    return {
        "rows": rows,
        "total": len(rows),
        "status_rows": status_rows,
        "type_rows": type_rows,
        "status_partition_valid": sum(int(row["count"]) for row in status_rows) == len(rows),
    }


def _dashboard_action_status_badge(status: str) -> str:
    colors = {
        "긴급 부족": ("#fee2e2", "#b91c1c"),
        "부족 주의": ("#ffedd5", "#c2410c"),
        "매출 감소": ("#dbeafe", "#1d4ed8"),
        "입고 지연": ("#fef3c7", "#a16207"),
        "과잉 후보": ("#ede9fe", "#6d28d9"),
    }
    background, foreground = colors.get(status, ("#e5e7eb", "#475569"))
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
        f'font-size:0.82rem;font-weight:600;background:{background};color:{foreground};">'
        f'{html.escape(status)}</span>'
    )


def _render_today_actions(facts: dict[str, Any], cache: dict[str, Any], *, render_mode: str) -> None:
    # 판정 기준은 목록의 작은 "기준:" 보조 문구로만 표시한다.
    state = _today_actions_presentation_state(facts.get("today_actions") or [])
    actions = state["rows"]
    if not actions:
        inventory = facts.get("inventory") or {}
        if not inventory or ("readiness_rows" not in inventory and "risk_targets" not in inventory):
            st.info("조치 판단에 필요한 자료가 부족합니다.")
        else:
            st.success("현재 우선 조치가 필요한 품목이 없습니다.")
        return
    interactive = render_mode == "primary"
    event_id = str(cache.get("dashboard_event_id") or "")
    amount_unit = _facts_amount_display_unit(facts)
    st.markdown(f"**오늘의 조치 TOP {state['total']:,}건**")
    st.markdown("**상태:** " + " ".join(f"`{row['label']} {int(row['count']):,}건`" for row in state["status_rows"]))
    if state["type_rows"]:
        st.markdown("**조치유형:** " + " ".join(f"`{row['label']} {int(row['count']):,}건`" for row in state["type_rows"]))
        st.caption("상태와 조치유형은 같은 TOP 목록을 서로 다른 기준으로 분류한 값이며 합산하지 않습니다.")

    actions_col, detail_col = st.columns([42, 58])
    with actions_col:
        st.markdown("#### 오늘의 우선 조치 TOP 10")
        st.caption("위험도와 업무 우선순위가 높은 조치를 순서대로 표시합니다.")
        with st.container(height=580, border=False):
            for fallback_index, action in enumerate(actions, start=1):
                rank = _safe_action_rank(action, fallback_index)
                status = _legacy_action_status(action)
                target = _legacy_action_target(action)
                value = action.get("evidence_value")
                unit = str(action.get("evidence_unit") or "")
                evidence_label = str(action.get("evidence_label") or "")
                evidence = evidence_label
                if value is not None and unit == "원":
                    evidence = f"{evidence_label} {_fmt_dashboard_amount(value, amount_unit)}"
                if not evidence:
                    evidence = str(action.get("evidence") or "-")
                threshold_label = str(action.get("threshold_label") or "")
                threshold_value = action.get("threshold_value")
                threshold_unit = str(action.get("threshold_unit") or "")
                cause_type = str(action.get("cause_type") or "")
                if threshold_value is None and action.get("stock_readiness_pct") not in (None, ""):
                    criterion = f"재고준비율 {_fmt_threshold_pct(action.get('stock_readiness_pct'))}%"
                elif threshold_value is None:
                    criterion = threshold_label or "-"
                elif cause_type in {"stock_shortage", "sales_decline"}:
                    criterion = f"{threshold_label} {_fmt_threshold_pct(threshold_value)}%"
                elif cause_type == "overstock_candidate":
                    criterion = f"{threshold_label} {_fmt_number(threshold_value)}"
                else:
                    criterion = f"{threshold_label} {_fmt_number(threshold_value)}{threshold_unit}".strip()
                supplements = []
                cover_days = action.get("stock_cover_days")
                if cover_days not in (None, ""):
                    supplements.append(f"재고커버 {_fmt_number(cover_days, 1)}일")
                if bool(action.get("inbound_delayed_candidate")):
                    supplements.append("입고 지연 후보")
                supplement = f" · {' · '.join(supplements[:2])}" if supplements else ""
                row_col, button_col = st.columns([84, 16])
                with row_col:
                    st.markdown(f"**{rank}.** {_dashboard_action_status_badge(status)} &nbsp; **{html.escape(target)}**", unsafe_allow_html=True)
                    st.caption(f"{html.escape(evidence)} · {html.escape(criterion)} · {html.escape(str(action.get('recommended_action') or '-'))}{html.escape(supplement)}")
                with button_col:
                    selection = (
                        _dashboard_action_detail_selection(
                            cache,
                            action,
                            facts=facts,
                        )
                        if interactive else None
                    )
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

    with detail_col:
        st.markdown("#### 선택 조치 상세")
        with st.container(height=580, border=False):
            selected = _selected_dashboard_action_detail(facts, cache, render_mode=render_mode)
            if not interactive:
                st.caption("상세 조치 정보는 현재 Dashboard 조회 세션에서만 확인할 수 있습니다.")
            elif not selected:
                st.info("조치 목록에서 [상세 보기]를 선택하세요.")
            else:
                matched_rows = _render_selected_dashboard_action_detail(facts, cache, render_mode=render_mode)
                if matched_rows:
                    instance_key = _risk_detail_instance_key(cache)
                    vendor_options, _ = _risk_detail_vendor_options(list(((facts.get("inventory") or {}).get("risk_detail_rows") or [])))
                    action_col, clear_col = st.columns(2)
                    with action_col:
                        st.button(
                            "전체 위험품목에서 보기",
                            key=f"__dashboard_lite_action_show_all::{event_id}",
                            on_click=_show_selected_action_in_all_risks,
                            args=(selected, matched_rows, instance_key, vendor_options),
                        )
                    with clear_col:
                        st.button(
                            "선택 해제",
                            key=f"__dashboard_lite_action_clear::{event_id}",
                            on_click=_clear_selected_dashboard_action,
                            args=(instance_key,),
                        )


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


def _selected_dashboard_action_detail(
    facts: dict[str, Any],
    cache: dict[str, Any],
    *,
    render_mode: str,
) -> dict[str, str] | None:
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
    action_id = str(selected.get("action_id") or "").strip()
    product_code = str(selected.get("product_code") or "").strip()
    action_id_present = bool(action_id)
    product_code_present = bool(product_code)
    action_pair_match = bool(
        action_id_present
        and product_code_present
        and _dashboard_action_detail_pair(facts, action_id=action_id, product_code=product_code) is not None
    )
    if not (all(matches.values()) and action_pair_match):
        st.session_state.pop("__dashboard_selected_action_detail", None)
        log.info(
            "[dashboard.action_detail] stage=discarded room_match=%s company_match=%s event_match=%s "
            "action_id_present=%s product_code_present=%s action_pair_match=%s detail_match_count=0 "
            "db_query_count=0 chat_push_count=0 suppress_autoscroll=False",
            matches["room_match"], matches["company_match"], matches["event_match"],
            action_id_present, product_code_present, action_pair_match,
        )
        return None
    return {key: str(value or "") for key, value in selected.items()}


def _render_selected_dashboard_action_detail(facts: dict[str, Any], cache: dict[str, Any], *, render_mode: str) -> list[dict[str, Any]]:
    """Render selected action rows from the current in-memory Dashboard facts only."""
    selected = _selected_dashboard_action_detail(facts, cache, render_mode=render_mode)
    if not selected:
        return []
    rows = list(((facts.get("inventory") or {}).get("risk_detail_rows") or []))
    product_code = str(selected["product_code"]).strip()
    matched_rows = [row for row in rows if isinstance(row, dict) and str(row.get("제품코드") or "").strip() == product_code]
    log.info(
        "[dashboard.action_detail] stage=rendered room_match=True company_match=True event_match=True "
        "action_id_present=True product_code_present=True detail_match_count=%s db_query_count=0 chat_push_count=0 suppress_autoscroll=False",
        len(matched_rows),
    )
    if not matched_rows:
        st.info("현재 Dashboard 상세자료에서 해당 품목을 찾지 못했습니다.")
        return []
    row = matched_rows[0]
    selected_action = _dashboard_action_detail_pair(
        facts,
        action_id=selected.get("action_id"),
        product_code=product_code,
    ) or {}
    status = str(row.get("위험상태") or "판정 제외")
    product_name = str(row.get("제품명") or product_code or "제품")
    st.markdown(f"{_dashboard_action_status_badge(status)} &nbsp; **{html.escape(product_name)}**", unsafe_allow_html=True)
    amount_unit = _facts_amount_display_unit(facts)
    minimum_replenishment_qty = _minimum_replenishment_qty(row.get("위험보정부족예상수량"))
    st.caption(f"제품코드 {str(row.get('제품코드') or '자료 없음')} · 주요매입처 {str(row.get('주요매입처명') or '자료 없음')}")
    values = (
        ("현재고", _fmt_number(row.get("현재재고수량"), 0) if row.get("현재재고수량") is not None else "자료 없음"),
        ("위험보정잔여예상수요", _fmt_number(row.get("위험보정잔여예상수요"), 2) if row.get("위험보정잔여예상수요") is not None else "자료 없음"),
        ("부족예상수량", _fmt_number(row.get("위험보정부족예상수량"), 2) if row.get("위험보정부족예상수량") is not None else "자료 없음"),
        ("최소보충수량", _fmt_number(minimum_replenishment_qty, 0) if minimum_replenishment_qty is not None else "자료 없음"),
        ("위험보정부족예상금액", _fmt_dashboard_amount(row.get("위험보정부족예상금액"), amount_unit) if row.get("위험보정부족예상금액") is not None else "자료 없음"),
        ("재고커버일", _fmt_number(row.get("재고커버일"), 1) if row.get("재고커버일") is not None else "자료 없음"),
        ("재고커버 자료상태", str(row.get("재고커버 자료상태") or "자료 부족")),
    )
    for chunk_start in range(0, len(values), 2):
        cols = st.columns(2)
        for col, (label, value) in zip(cols, values[chunk_start:chunk_start + 2]):
            with col:
                st.caption(label)
                st.markdown(f"**{html.escape(str(value))}**")
    st.caption(
        " · ".join((
            f"준비율 {(_fmt_threshold_pct(row.get('위험보정재고준비율')) + '%') if row.get('위험보정재고준비율') is not None else '자료 없음'}",
            f"위험사유 {str(row.get('위험사유') or '자료 없음')}",
            f"수요급증 {'수요급증' if bool(row.get('수요급증여부')) else '해당 없음'}",
            f"최근 정상 입고일 {str(row.get('최근 정상 입고일') or '자료 없음')}",
            f"입고 경과일 {str(row.get('입고 경과일') or '자료 없음')}",
            f"과잉·저활성 근거 {str(row.get('과잉·저활성 근거') or '자료 없음')}",
            f"최근 정상 출고일 {str(row.get('최근 정상 출고일') or '자료 연결 필요')}",
            f"출고 자료상태 {str(row.get('출고 자료상태') or '자료 연결 필요')}",
        ))
    )
    st.markdown(f"**권장 조치: {html.escape(str(selected_action.get('recommended_action') or '자료 없음'))}**")
    return matched_rows


def _risk_detail_filter_keys(instance_key: str) -> dict[str, str]:
    return {
        "mode": f"__dashboard_lite_risk_detail_mode::{instance_key}",
        "toggle": f"__dashboard_lite_risk_detail_toggle::{instance_key}",
        "status": f"__dashboard_lite_risk_detail_status::{instance_key}",
        "vendor": f"__dashboard_lite_risk_detail_vendor::{instance_key}",
        "surge": f"__dashboard_lite_risk_detail_surge::{instance_key}",
        "zero": f"__dashboard_lite_risk_detail_zero::{instance_key}",
        "search": f"__dashboard_lite_risk_detail_search::{instance_key}",
        "limit": f"__dashboard_lite_risk_detail_limit::{instance_key}",
        "notice": f"__dashboard_lite_risk_detail_sync_notice::{instance_key}",
    }


def _show_selected_action_in_all_risks(
    selection: dict[str, str] | None,
    matched_rows: list[dict[str, Any]],
    instance_key: str,
    vendor_options: list[str],
) -> None:
    """Apply selection-derived filters only after the user explicitly asks for them."""
    if not isinstance(selection, dict) or not matched_rows:
        return
    row = matched_rows[0]
    keys = _risk_detail_filter_keys(instance_key)
    product_code = str(row.get("제품코드") or selection.get("product_code") or "").strip()
    status = str(row.get("위험상태") or "")
    vendor_key = str(row.get("_주요매입처필터키") or "")
    st.session_state[keys["mode"]] = "all_risk_items"
    st.session_state[keys["toggle"]] = True
    st.session_state[keys["search"]] = product_code
    st.session_state[keys["status"]] = status if status in {"긴급 부족", "부족 주의"} else "전체 위험"
    st.session_state[keys["vendor"]] = vendor_key if vendor_key in vendor_options else "전체"
    st.session_state[keys["surge"]] = "수요급증" if bool(row.get("수요급증여부")) else "전체"
    if float(row.get("위험보정부족예상금액") or 0) <= 0:
        st.session_state[keys["zero"]] = True
    st.session_state[keys["notice"]] = True
    _request_dashboard_scroll_suppression("selected_action_all_risk")


def _clear_selected_dashboard_action(instance_key: str) -> None:
    st.session_state.pop("__dashboard_selected_action_detail", None)
    st.session_state.pop(f"__dashboard_lite_risk_detail_open_request::{instance_key}", None)
    _request_dashboard_scroll_suppression("selected_action_clear")


def _reset_risk_detail_filters(instance_key: str) -> None:
    keys = _risk_detail_filter_keys(instance_key)
    st.session_state[keys["status"]] = "전체 위험"
    st.session_state[keys["vendor"]] = "전체"
    st.session_state[keys["surge"]] = "전체"
    st.session_state[keys["zero"]] = True
    st.session_state[keys["search"]] = ""
    st.session_state.pop(keys["notice"], None)
    _request_dashboard_scroll_suppression("risk_detail_reset")


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


_RISK_DETAIL_DISPLAY_COLUMNS = (
    "순번",
    "위험상태",
    "위험사유",
    "제품코드",
    "제품명",
    "규격",
    "제조사명",
    "주요매입처명",
    "현재재고수량",
    "위험보정잔여예상수요",
    "위험보정부족예상수량",
    "최소보충수량",
    "위험보정부족예상금액",
    "위험보정재고준비율",
    "재고커버일",
    "재고커버 자료상태",
    "과잉후보여부",
    "과잉·저활성 근거",
    "최근 정상 출고일",
    "출고 경과일",
    "출고 자료상태",
    "수요급증세부분류",
    "최근 정상 입고일",
    "입고 경과일",
    "정상 입고 거래일수",
    "평균 입고간격일",
    "입고 자료상태",
    "입고 지연후보",
    "최근입고 대표매입처명",
    "최근입고 대표매입처코드",
    "최근입고 대표매입처출처",
    "최근365일 입고이력",
)

_RISK_DETAIL_PINNED_COLUMNS = {
    "순번",
    "위험상태",
    "위험사유",
    "제품코드",
    "제품명",
    "규격",
}

_RISK_DETAIL_OUTBOUND_COLUMNS = {
    "최근 정상 출고일",
    "출고 경과일",
    "출고 자료상태",
}

_RISK_DETAIL_DISPLAY_TEXT_COLUMNS = {
    "위험상태",
    "위험사유",
    "제품코드",
    "제품명",
    "규격",
    "제조사명",
    "주요매입처명",
    "재고커버 자료상태",
    "과잉후보여부",
    "과잉·저활성 근거",
    "최근 정상 출고일",
    "출고 자료상태",
    "수요급증세부분류",
    "최근 정상 입고일",
    "입고 자료상태",
    "입고 지연후보",
    "최근입고 대표매입처명",
    "최근입고 대표매입처코드",
    "최근입고 대표매입처출처",
    "최근365일 입고이력",
}


_RISK_DETAIL_DISPLAY_LIMIT_OPTIONS = (10, 50, 100, 300, 500)
_RISK_DETAIL_DEFAULT_DISPLAY_LIMIT = 100


def _normalize_risk_detail_display_limit(value: Any) -> int:
    """Keep legacy valid widget values while safely recovering invalid state."""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return _RISK_DETAIL_DEFAULT_DISPLAY_LIMIT
    return limit if limit in _RISK_DETAIL_DISPLAY_LIMIT_OPTIONS else _RISK_DETAIL_DEFAULT_DISPLAY_LIMIT


def _risk_detail_initial_display_limit(cache: dict[str, Any] | None) -> int:
    """Resolve the presentation-only initial row count without touching facts."""
    raw = st.session_state.get("__dashboard_lite_risk_quick_view_count")
    if raw is None:
        raw = ((cache or {}).get("params") or {}).get("risk_quick_view_count")
    if raw is None:
        raw = _DASHBOARD_PROFILE_SCALAR_DEFAULTS["risk_quick_view_count"]
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return int(_DASHBOARD_PROFILE_SCALAR_DEFAULTS["risk_quick_view_count"])


def _risk_detail_display_summary(filtered_rows: int, display_rows: int) -> str:
    return f"필터 결과 {int(filtered_rows):,}건 중 상위 {int(display_rows):,}건 표시"


def _risk_detail_display_height(display_rows: int) -> int:
    return min(560, max(220, 72 + int(display_rows) * 35))


def _risk_detail_has_outbound_facts(filtered: pd.DataFrame) -> bool:
    """Keep unavailable outbound placeholders out of the primary detail grid."""
    if filtered.empty:
        return False
    if "출고 자료상태" in filtered.columns:
        statuses = filtered["출고 자료상태"].fillna("").astype(str).str.strip()
        populated_statuses = statuses.loc[statuses.ne("")]
        return bool(populated_statuses.ne("자료 연결 필요").any())
    if "최근 정상 출고일" in filtered.columns:
        dates = filtered["최근 정상 출고일"].fillna("").astype(str).str.strip()
        return bool(dates.ne("").any())
    return False


def _minimum_replenishment_qty(value: Any) -> int | None:
    """Return the display-only unit-level replenishment reference quantity."""
    try:
        shortage_qty = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(shortage_qty):
        return None
    return max(0, math.ceil(shortage_qty))


def _build_risk_detail_display_frame(filtered: pd.DataFrame, display_limit: int) -> pd.DataFrame:
    """Build a presentation-only slice without changing cached or export facts."""
    limit = max(0, int(display_limit or 0))
    display = filtered.head(limit).copy()
    display.insert(0, "순번", range(1, len(display) + 1))
    shortage_qty = pd.to_numeric(display.get("위험보정부족예상수량"), errors="coerce")
    if not isinstance(shortage_qty, pd.Series):
        shortage_qty = pd.Series(index=display.index, dtype="float64")
    minimum_replenishment = (-shortage_qty.clip(lower=0)).floordiv(1).mul(-1)
    display["최소보충수량"] = minimum_replenishment.astype("Int64")

    if "과잉후보여부" in display.columns:
        overstock_candidate = display["과잉후보여부"].eq(True)
        display["과잉후보여부"] = overstock_candidate.map({True: "예", False: ""})
        if "과잉·저활성 근거" in display.columns:
            display.loc[~overstock_candidate, "과잉·저활성 근거"] = ""
    if "재고커버 자료상태" in display.columns:
        display["재고커버 자료상태"] = display["재고커버 자료상태"].replace({
            "ready": "계산 가능",
            "zero_stock": "재고 없음",
            "no_demand": "잔여수요 없음",
            "insufficient_data": "자료 부족",
            "closed_horizon": "평가기간 종료",
        })

    hidden_columns = set()
    if not _risk_detail_has_outbound_facts(filtered):
        hidden_columns.update(_RISK_DETAIL_OUTBOUND_COLUMNS)
    visible_columns = [
        column for column in _RISK_DETAIL_DISPLAY_COLUMNS
        if column in display.columns and column not in hidden_columns
    ]
    display = display.loc[:, visible_columns]

    for column in _RISK_DETAIL_DISPLAY_TEXT_COLUMNS.intersection(display.columns):
        values = display[column]
        cleaned = values.where(values.notna(), "").astype(str).str.strip()
        display[column] = cleaned.mask(cleaned.str.casefold().isin({"none", "nan", "nat"}), "")

    for date_column in ("최근 정상 입고일", "최근 정상 출고일"):
        if date_column not in display.columns:
            continue
        values = display[date_column].astype(str).str.strip()
        valid_yyyymmdd = values.str.fullmatch(r"\d{8}")
        display.loc[valid_yyyymmdd, date_column] = (
            values.loc[valid_yyyymmdd].str.slice(0, 4)
            + "-"
            + values.loc[valid_yyyymmdd].str.slice(4, 6)
            + "-"
            + values.loc[valid_yyyymmdd].str.slice(6, 8)
        )
    return display


def _risk_detail_display_column_config() -> dict[str, Any]:
    return {
        "순번": st.column_config.NumberColumn("순번", format="%d", width=60, pinned=True, alignment="center"),
        "위험상태": st.column_config.TextColumn("위험상태", width=100, pinned=True, alignment="center"),
        "위험사유": st.column_config.TextColumn("위험사유", width=150, pinned=True),
        "제품코드": st.column_config.TextColumn("제품코드", width=90, pinned=True, alignment="center"),
        "제품명": st.column_config.TextColumn("제품명", width=280, pinned=True),
        "규격": st.column_config.TextColumn("규격", width=90, pinned=True, alignment="center"),
        "제조사명": st.column_config.TextColumn("제조사명", width=150),
        "주요매입처명": st.column_config.TextColumn("주요매입처명", width=230),
        "현재재고수량": st.column_config.NumberColumn("현재고", format="%,.0f", width=100, alignment="right"),
        "위험보정잔여예상수요": st.column_config.NumberColumn("잔여예상수요", format="%,.2f", width=120, alignment="right"),
        "위험보정부족예상수량": st.column_config.NumberColumn("부족예상수량", format="%,.2f", width=120, alignment="right"),
        "최소보충수량": st.column_config.NumberColumn(
            "최소보충수량",
            format="%,.0f",
            width=120,
            alignment="right",
            help="부족예상수량을 제품 단위로 올림한 최소 보충 참고수량입니다. 실제 발주수량은 포장·발주단위를 확인해야 합니다.",
        ),
        "위험보정부족예상금액": st.column_config.NumberColumn("부족예상금액", format="%,.0f", width=130, alignment="right"),
        "위험보정재고준비율": st.column_config.NumberColumn("재고준비율", format="%.2f%%", width=110, alignment="right"),
        "재고커버일": st.column_config.NumberColumn("커버일", format="%,.1f", width=90, alignment="right"),
        "재고커버 자료상태": st.column_config.TextColumn("커버상태", width=120, alignment="center"),
        "과잉후보여부": st.column_config.TextColumn("과잉후보", width=80, alignment="center"),
        "과잉·저활성 근거": st.column_config.TextColumn("과잉근거", width=220),
        "최근 정상 출고일": st.column_config.TextColumn("최근 정상 출고일", width=120, alignment="center"),
        "출고 경과일": st.column_config.NumberColumn("출고 경과일", format="%,.0f", width=100, alignment="right"),
        "출고 자료상태": st.column_config.TextColumn("출고 자료상태", width=120, alignment="center"),
        "수요급증세부분류": st.column_config.TextColumn("수요급증유형", width=150),
        "최근 정상 입고일": st.column_config.TextColumn("최근 정상 입고일", width=120, alignment="center"),
        "입고 경과일": st.column_config.NumberColumn("입고 경과일", format="%,.0f", width=100, alignment="right"),
        "정상 입고 거래일수": st.column_config.NumberColumn("입고거래일수", format="%,.0f", width=110, alignment="right"),
        "평균 입고간격일": st.column_config.NumberColumn("평균입고간격", format="%,.2f", width=120, alignment="right"),
        "입고 자료상태": st.column_config.TextColumn("입고 자료상태", width=110, alignment="center"),
        "입고 지연후보": st.column_config.TextColumn("입고 지연후보", width=100, alignment="center"),
    }


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
    threshold = _fmt_threshold_pct(_dashboard_readiness_threshold(facts, params))
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
        {"조건명": "판단 기준일", "값": str(params.get("policy_date") or "")},
        *supplier_conditions,
        {"조건명": "재고기준", "값": stock_mode},
        {"조건명": "대상 재고위치", "값": ", ".join(stock_labels) if stock_labels else "전체"},
        {"조건명": "재고준비율 경고기준", "값": f"{threshold}%"},
        {"조건명": "주요매입처 기준기간", "값": f"최근 {int(vendor_summary.get('basis_days') or params.get('major_purchase_vendor_days') or _DASHBOARD_PROFILE_SCALAR_DEFAULTS['major_purchase_vendor_days'])}일"},
        {"조건명": "주요매입처 판단 기준일", "값": str(vendor_summary.get("basis_cutoff_date") or "")},
        {"조건명": "주요매입처 기준", "값": "정상 입고수량 최대, 기간 내 입고 없음 시 제품마스터 발주처"},
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
    st.caption("재고커버일은 현재 평가월의 위험보정 잔여예상수요를 기준으로 계산한 재고 보유 가능 일수입니다.")
    summary_cols = st.columns(3)
    for column, (label, key) in zip(summary_cols, (
        ("전체 위험품목", "source_rows"), ("긴급 부족", "emergency_rows"), ("부족 주의", "warning_rows"),
        ("금액 양수", "amount_positive_rows"), ("금액 0", "zero_amount_rows"),
    )):
        with column:
            _metric_card(label, summary.get(key, 0), "개")

    st.caption(
        f"금액 양수 {int(summary.get('amount_positive_rows') or 0):,}건 · "
        f"금액 0 {int(summary.get('zero_amount_rows') or 0):,}건"
    )
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
    filter_keys = _risk_detail_filter_keys(instance_key)
    toggle_key = filter_keys["toggle"]
    search_key = filter_keys["search"]
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
    st.session_state[filter_keys["mode"]] = "all_risk_items"

    if bool(st.session_state.pop(filter_keys["notice"], False)):
        st.caption("선택한 조치 품목 조건으로 전체 위험품목을 표시합니다.")

    filter_cols = st.columns((16, 34, 18, 32))
    with filter_cols[0]:
        risk_status = st.selectbox("위험상태", ["전체 위험", "긴급 부족", "부족 주의"], key=f"__dashboard_lite_risk_detail_status::{instance_key}", on_change=_request_dashboard_scroll_suppression, args=("local_filter",))
    with filter_cols[1]:
        vendor_key = st.selectbox("주요매입처", vendor_options, format_func=lambda value: vendor_labels.get(value, "매입처 미확인"), key=f"__dashboard_lite_risk_detail_vendor::{instance_key}", on_change=_request_dashboard_scroll_suppression, args=("local_filter",))
    with filter_cols[2]:
        surge_filter = st.selectbox("수요급증", ["전체", "수요급증", "일반"], key=f"__dashboard_lite_risk_detail_surge::{instance_key}", on_change=_request_dashboard_scroll_suppression, args=("local_filter",))
    with filter_cols[3]:
        search_text = st.text_input("제품 검색", key=search_key, on_change=_request_dashboard_scroll_suppression, args=("local_filter",))

    limit_key = filter_keys["limit"]
    initial_display_limit = _risk_detail_initial_display_limit(cache)
    display_limit_options = sorted(set((*_RISK_DETAIL_DISPLAY_LIMIT_OPTIONS, initial_display_limit)))
    if limit_key not in st.session_state:
        st.session_state[limit_key] = initial_display_limit
    elif int(st.session_state.get(limit_key) or 0) not in display_limit_options:
        st.session_state[limit_key] = initial_display_limit

    utility_cols = st.columns((16, 20, 14, 50), vertical_alignment="bottom")
    with utility_cols[0]:
        include_zero_amount = st.toggle("금액 0 포함", value=True, key=f"__dashboard_lite_risk_detail_zero::{instance_key}", on_change=_request_dashboard_scroll_suppression, args=("local_filter",))
    with utility_cols[1]:
        display_limit = st.selectbox(
            "화면 표시 행 수",
            display_limit_options,
            index=display_limit_options.index(initial_display_limit),
            key=limit_key,
            on_change=_request_dashboard_scroll_suppression,
            args=("local_filter",),
        )
    with utility_cols[2]:
        st.button(
            "필터 초기화",
            key=f"__dashboard_lite_risk_detail_reset::{instance_key}",
            on_click=_reset_risk_detail_filters,
            args=(instance_key,),
        )
    filtered, filter_summary, elapsed_ms = filter_dashboard_risk_detail_rows(
        rows,
        risk_status=risk_status,
        vendor_key=vendor_key,
        surge_filter=surge_filter,
        include_zero_amount=include_zero_amount,
        search_text=search_text,
    )
    display_limit = int(display_limit)
    display = _build_risk_detail_display_frame(filtered, display_limit)
    log.info(
        "[dashboard.risk_detail] source_rows=%s filtered_rows=%s displayed_rows=%s emergency_rows=%s warning_rows=%s zero_amount_rows=%s vendor_filter_applied=%s surge_filter_applied=%s search_filter_applied=%s include_zero_amount=%s display_limit=%s elapsed_ms=%s",
        filter_summary["source_rows"], filter_summary["filtered_rows"], len(display), filter_summary["emergency_rows"], filter_summary["warning_rows"], filter_summary["zero_amount_rows"],
        vendor_key != "전체", surge_filter != "전체", bool(str(search_text or "").strip()), include_zero_amount, display_limit, elapsed_ms,
    )
    with utility_cols[3]:
        st.caption(_risk_detail_display_summary(filter_summary["filtered_rows"], len(display)))
    if not _risk_detail_has_outbound_facts(filtered):
        st.caption("최근 정상 출고일은 일자 단위 원천 연결 후 제공됩니다.")
    st.dataframe(
        display,
        width="stretch",
        height=_risk_detail_display_height(len(display)),
        hide_index=True,
        column_config=_risk_detail_display_column_config(),
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
        "overstock_inactive_days": params.get("overstock_inactive_days"),
        "readiness_warning_pct": params.get("readiness_warning_pct"),
        "risk_quick_view_count": params.get("risk_quick_view_count"),
        "amount_display_unit": params.get("amount_display_unit"),
        "inbound_cycle_days": 365,
        "inbound_vendor_days": params.get("major_purchase_vendor_days"),
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


def _render_dashboard_scope_form_contents() -> tuple[bool, bool, dict[str, Any] | None]:
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
    risk_analysis_days = st.session_state.get(
        "__dashboard_lite_risk_analysis_days",
        _DASHBOARD_PROFILE_SCALAR_DEFAULTS["risk_analysis_days"],
    )
    readiness = st.session_state.get(
        "__dashboard_lite_readiness_warning_pct",
        _DASHBOARD_PROFILE_SCALAR_DEFAULTS["readiness_warning_pct"],
    )
    io_count = len(_clean_list(st.session_state.get("__dashboard_lite_io_gu_list")))
    io_summary = "\uc785\ucd9c\uace0 \uc804\uccb4" if io_count == 0 else f"\uc785\ucd9c\uace0 {io_count}\uac1c"
    condition_summary = f"{stock_basis} \u00b7 \uc7ac\uace0\uc704\uce58 {stock_scope} \u00b7 {io_summary} \u00b7 \uc900\ube44\uc728 {readiness}%"
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
        )
    rendered_scope_mode_key = "__dashboard_lite_supplier_mode_rendered"
    rendered_scope_mode = str(st.session_state.get(rendered_scope_mode_key) or scope_mode)
    supplier_mode_changed = rendered_scope_mode != scope_mode
    st.session_state[rendered_scope_mode_key] = scope_mode
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

        row4 = st.columns(4)
        with row4[0]:
            major_purchase_vendor_days = st.number_input("대표 매입처 기준기간(일)", min_value=1, step=1, key="__dashboard_lite_major_purchase_vendor_days")
        with row4[1]:
            overstock_inactive_days = st.number_input("과잉·저활성 기준(일)", min_value=1, step=1, key="__dashboard_lite_overstock_inactive_days")
        with row4[2]:
            readiness_warning_pct = st.number_input("준비율 경고기준(%)", min_value=0.1, max_value=100.0, step=0.1, key="__dashboard_lite_readiness_warning_pct")
        with row4[3]:
            risk_quick_view_count = st.number_input("위험품목 바로보기", min_value=1, step=1, key="__dashboard_lite_risk_quick_view_count")
    submitted = st.form_submit_button("대시보드 조회", type="primary", width="stretch")
    try:
        from app.ui.ssai_login import has_permission
        save_requested = st.form_submit_button("저장", width="stretch") if has_permission(PROFILE_PERMISSION) else False
    except Exception:
        save_requested = False
    supplier_result = {"status": "all", "codes": [], "names": [], "label": "전체"}
    if (submitted or save_requested) and supplier_mode_changed:
        st.info("공급 기준이 변경되었습니다. 새 제약사/발주처와 담당자 조건을 확인한 뒤 다시 실행해 주세요.")
        return False, False, None
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
        "io_gu_list": [str(value).split(":", 1)[-1] for value in _clean_list(io_gu)],
        "io_gu_source": "screen",
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


def _render_dashboard_scope_form() -> tuple[bool, bool, dict[str, Any] | None]:
    """Render every Dashboard-affecting draft control in one atomic form."""
    with st.form("dashboard_lite_scope_form",
        clear_on_submit=False,
        enter_to_submit=False,
    ):
        return _render_dashboard_scope_form_contents()


def _dashboard_scope_header(params: dict[str, Any]) -> str:
    """Return the concise, user-facing scope line for the dedicated result block."""
    parts = [
        f"조회기간: {params.get('month_from') or '-'}~{params.get('month_to') or '-'}",
        f"평가월: {params.get('evaluation_month') or '-'}",
        f"판단 기준일: {params.get('policy_date') or '-'}",
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


def _compact_dashboard_rows(rows: Any, *, allowed_keys: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    """Project persisted dashboard rows without retaining drill-down payloads."""
    compact_rows: list[dict[str, Any]] = []
    for row in list(rows or [])[:limit]:
        if not isinstance(row, dict):
            continue
        compact_rows.append({key: row.get(key) for key in allowed_keys if key in row})
    return compact_rows


_DASHBOARD_COMPACT_RISK_TARGET_KEYS = (
    "product_code", "product_name", "재고위험상태", "current_stock_qty", "주요매입처명",
    "수요급증여부", "위험보정기준", "위험보정재고준비율", "stock_readiness_pct",
    "위험보정잔여예상수요", "remaining_expected_demand_qty", "위험보정부족예상수량",
    "shortage_qty", "위험보정부족예상금액", "shortage_amt",
)

_DASHBOARD_COMPACT_VENDOR_RISK_KEYS = (
    "주요매입처명", "주요매입처코드", "긴급부족금액", "부족주의금액", "전체위험보정부족금액",
    "긴급부족품목수", "부족주의품목수", "위험품목수", "수요급증품목수",
)

_DASHBOARD_COMPACT_ACTION_KEYS = (
    "priority", "rank", "status", "risk_grade", "action_id", "product_code", "product_name",
    "target", "target_name", "evidence_value", "evidence_unit", "evidence_label", "evidence",
    "threshold_label", "threshold_value", "threshold_unit", "cause_type", "stock_readiness_pct", "recommended_action",
    "stock_cover_days", "inbound_delayed_candidate",
)


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
    compact_transaction_cycle = dict(facts.get("transaction_cycle") or facts.get("turnover_days") or {})
    if compact_transaction_cycle and not compact_transaction_cycle.get("status"):
        compact_transaction_cycle["status"] = resolve_transaction_cycle_status(
            compact_transaction_cycle.get("purchase_result_status") or compact_transaction_cycle.get("purchase_data_status"),
            compact_transaction_cycle.get("sales_result_status") or compact_transaction_cycle.get("sales_data_status"),
        )
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
            "decline_targets": _compact_dashboard_rows(
                sales.get("decline_targets"),
                allowed_keys=("product_code", "product_name", "current_month_amount", "previous_month_amount", "change_rate"),
                limit=row_limit,
            ),
        },
        "inventory": {
            "metrics": dict(inventory.get("metrics") or {}),
            "risk_targets": _compact_dashboard_rows(
                inventory.get("risk_targets"),
                allowed_keys=_DASHBOARD_COMPACT_RISK_TARGET_KEYS,
                limit=row_limit,
            ),
            "stock_risk_summary": list(inventory.get("stock_risk_summary") or []),
            "inventory_status_summary": {
                key: (inventory.get("inventory_status_summary") or {}).get(key)
                for key in (
                    "total_product_count", "expected_demand_product_count", "status_counts",
                    "frequency_counts", "snapshot_status", "snapshot_generation_no",
                    "snapshot_checksum", "snapshot_reason", "missing_frequency_product_count",
                )
            },
            "stock_overstock_summary": dict(inventory.get("stock_overstock_summary") or {}),
            "stock_demand_surge_summary": dict(inventory.get("stock_demand_surge_summary") or {}),
            "vendor_stock_risk_summary": dict(inventory.get("vendor_stock_risk_summary") or {}),
            "vendor_stock_risk_top_rows": _compact_dashboard_rows(
                inventory.get("vendor_stock_risk_top_rows"),
                allowed_keys=_DASHBOARD_COMPACT_VENDOR_RISK_KEYS,
                limit=row_limit,
            ),
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
            "visual_phase2_summary": {
                key: (inventory.get("visual_phase2_summary") or {}).get(key)
                for key in (
                    "inventory_count", "evaluation_remaining_days", "cover_zero_stock_count",
                    "cover_shortfall_count", "cover_sufficient_count", "cover_no_demand_count",
                    "cover_insufficient_count", "cover_partition_valid", "inbound_delay_candidate_count",
                    "overstock_candidate_count", "recent_purchase_none_count", "vendor_unknown_count",
                    "demand_surge_count", "action_count", "purchase_trend_status", "purchase_trend_points",
                    "vendor_top_count", "additional_source_call_count", "briefing_lines",
                )
            },
            "purchase_trend_rows": list(inventory.get("purchase_trend_rows") or [])[:18],
            "data_quality": list(inventory.get("data_quality") or [])[:row_limit],
        },
        "turnover_days": dict(compact_transaction_cycle),
        "transaction_cycle": dict(compact_transaction_cycle),
        "today_actions": _compact_dashboard_rows(
            facts.get("today_actions"),
            allowed_keys=_DASHBOARD_COMPACT_ACTION_KEYS,
            limit=row_limit,
        ),
        "data_quality": list(facts.get("data_quality") or [])[:row_limit],
        "performance": {
            key: (facts.get("performance") or {}).get(key)
            for key in (
                "logical_source_count",
                "physical_query_count",
                "physical_query_count_total",
                "physical_query_count_by_source",
                "sales_source_elapsed_ms",
                "stock_source_elapsed_ms",
                "inbound_source_elapsed_ms",
                "post_process_elapsed_ms",
                "total_elapsed_ms",
                "measured_phase_total_ms",
                "unaccounted_elapsed_ms",
                "unaccounted_ratio_pct",
            )
            if key in (facts.get("performance") or {})
        },
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


def _dashboard_layout_preview_key(cache: dict[str, Any]) -> str:
    # This is a room/session display preference, not an event-payload field.
    return "__dashboard_layout_v2_preview"


def _dashboard_render_namespace(cache: dict[str, Any] | None, *, render_mode: str) -> str:
    """Return a stable per-message/per-render-location Streamlit key namespace."""
    source = cache if isinstance(cache, dict) else {}
    event_id = str(source.get("dashboard_event_id") or source.get("id") or "").strip()
    if event_id:
        identity: Any = {
            "event_id": event_id,
            "room_id": str(source.get("room_id") or ""),
            "company_id": str(source.get("company_id") or ""),
        }
    else:
        identity = {
            "cache_key": str(source.get("cache_key") or ""),
            "query_fingerprint": str(source.get("query_fingerprint") or ""),
            "room_id": str(source.get("room_id") or ""),
            "company_id": str(source.get("company_id") or ""),
            "created_at": str(source.get("created_at") or ""),
            "params": source.get("params") if isinstance(source.get("params"), dict) else {},
        }
    identity_text = json.dumps(identity, ensure_ascii=True, sort_keys=True, default=str)
    digest = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()[:20]
    safe_mode = re.sub(r"[^a-z0-9_-]", "", str(render_mode or "chat").lower()) or "chat"
    return f"{safe_mode}_{digest}"


def _dashboard_layout_v2_enabled(cache: dict[str, Any] | None) -> bool:
    cache = cache or {}
    key = _dashboard_layout_preview_key(cache)
    if not _DASHBOARD_V2_PREVIEW_AVAILABLE:
        # A stale preview selection must not survive the temporary rollback.
        st.session_state.pop(key, None)
        return False
    return bool(st.session_state.get(key, False))


def _render_dashboard_layout_preview_toggle(cache: dict[str, Any], *, render_mode: str) -> None:
    """Render the preview switch without changing cached facts or query state."""
    key = _dashboard_layout_preview_key(cache)
    if not _DASHBOARD_V2_PREVIEW_AVAILABLE:
        st.session_state.pop(key, None)
        log.info(
            "[dashboard.layout_preview] available=False enabled=False render_mode=%s",
            render_mode,
        )
        return
    if key not in st.session_state:
        st.session_state[key] = False
    if render_mode == "primary":
        st.toggle("새 레이아웃 미리보기", key=key)
    log.info(
        "[dashboard.layout_preview] enabled=%s layout_mode=%s render_mode=%s room_id_present=%s "
        "event_id_present=%s compact_mode=%s physical_query_count_delta=0 source_call_count_delta=0",
        bool(st.session_state.get(key)),
        "v2" if bool(st.session_state.get(key)) else "legacy",
        render_mode,
        bool(str(cache.get("room_id") or "")),
        bool(str(cache.get("dashboard_event_id") or "")),
        render_mode != "primary",
    )


def _dashboard_analysis_detail_key(cache: dict[str, Any]) -> str:
    event_id = str(cache.get("dashboard_event_id") or cache.get("id") or "active").strip()
    return f"__dashboard_analysis_detail_context::{event_id or 'active'}"


def _set_dashboard_analysis_detail_context(cache: dict[str, Any], context: str, status: str = "") -> None:
    st.session_state[_dashboard_analysis_detail_key(cache)] = {
        "context": context,
        "status": status,
        "event_id": str(cache.get("dashboard_event_id") or ""),
        "room_id": str(cache.get("room_id") or ""),
    }
    _request_dashboard_scroll_suppression("analysis_detail_context")


def _dashboard_analysis_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return ""


def _dashboard_analysis_rows(facts: dict[str, Any], context: str) -> list[dict[str, Any]]:
    inventory = facts.get("inventory") or {}
    if context == "risk":
        return [row for row in inventory.get("risk_detail_rows") or [] if isinstance(row, dict)]
    rows = [row for row in inventory.get("readiness_rows") or [] if isinstance(row, dict)]
    if context == "demand":
        return [
            row for row in rows
            if bool(_dashboard_analysis_value(row, "수요급증여부", "demand_surge"))
        ]
    return rows


def _dashboard_analysis_status(row: dict[str, Any], context: str) -> str:
    if context == "risk":
        return str(_dashboard_analysis_value(row, "재고위험상태", "위험상태", "status") or "기타")
    if context == "demand":
        return str(_dashboard_analysis_value(row, "수요급증상위분류", "수요급증세부분류", "수요 변화") or "수요 변화")
    cover = str(_dashboard_analysis_value(row, "stock_cover_status", "재고커버상태") or "").strip()
    if cover:
        return cover
    if bool(_dashboard_analysis_value(row, "inbound_delayed_candidate", "입고 지연후보")):
        return "입고지연 후보"
    if bool(_dashboard_analysis_value(row, "과잉후보여부")):
        return "과잉 후보"
    if str(_dashboard_analysis_value(row, "주요매입처상태")) == "recent_purchase_none":
        return "최근 매입 없음"
    return "전체"


def _dashboard_analysis_frame(rows: list[dict[str, Any]], context: str) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for row in rows:
        records.append({
            "분석 상태": _dashboard_analysis_status(row, context),
            "제품코드": _dashboard_analysis_value(row, "product_code", "제품코드"),
            "제품명": _dashboard_analysis_value(row, "product_name", "제품명"),
            "주요매입처": _dashboard_analysis_value(row, "주요매입처명", "major_purchase_vendor_name"),
            "현재고": _dashboard_analysis_value(row, "current_stock_qty", "현재재고수량"),
            "예상수요": _dashboard_analysis_value(row, "remaining_expected_demand_qty", "위험보정잔여예상수요"),
            "부족예상수량": _dashboard_analysis_value(row, "shortage_qty", "위험보정부족예상수량"),
            "부족예상금액": _dashboard_analysis_value(row, "shortage_amt", "위험보정부족예상금액"),
            "재고준비율": _dashboard_analysis_value(row, "stock_readiness_pct", "위험보정재고준비율"),
            "재고커버일": _dashboard_analysis_value(row, "stock_cover_days", "재고커버일수"),
            "최근 입고일": _dashboard_analysis_value(row, "최근 정상 입고일", "last_normal_inbound_date"),
            "분석 사유": _dashboard_analysis_value(row, "위험사유", "수요급증세부분류사유", "과잉후보사유"),
        })
    return pd.DataFrame(records)


def _render_dashboard_analysis_detail(facts: dict[str, Any], cache: dict[str, Any], *, render_mode: str) -> None:
    """Render one facts-only detail table for risk, follow-up, and demand contexts."""
    selected = st.session_state.get(_dashboard_analysis_detail_key(cache))
    context = str((selected or {}).get("context") or "risk") if isinstance(selected, dict) else "risk"
    if context not in {"risk", "followup", "demand"}:
        context = "risk"
    if render_mode != "primary":
        rows = _dashboard_analysis_rows(facts, context)
        st.caption(f"{context} 분석 품목 요약 {len(rows):,}건 - 상세표와 Excel은 현재 Dashboard 조회에서만 제공합니다.")
        return

    rows = _dashboard_analysis_rows(facts, context)
    frame = _dashboard_analysis_frame(rows, context)
    status_options = ["전체"] + sorted({str(value) for value in frame.get("분석 상태", pd.Series(dtype=str)).tolist() if str(value).strip()})
    instance = _dashboard_analysis_detail_key(cache)
    vendor_options = ["all"] + sorted({
        str(_dashboard_analysis_value(row, "major_purchase_vendor_name"))
        for row in rows
        if str(_dashboard_analysis_value(row, "major_purchase_vendor_name")).strip()
    })
    controls = st.columns((18, 24, 24, 22, 12))
    with controls[0]:
        selected_context = st.selectbox(
            "분석 유형", ["risk", "followup", "demand"],
            format_func=lambda value: {"risk": "위험 품목", "followup": "재고 후속관리", "demand": "수요 변화"}[value],
            key=f"{instance}::selector",
        )
    if selected_context != context:
        _set_dashboard_analysis_detail_context(cache, selected_context)
        context = selected_context
        rows = _dashboard_analysis_rows(facts, context)
        frame = _dashboard_analysis_frame(rows, context)
        status_options = ["전체"] + sorted({str(value) for value in frame.get("분석 상태", pd.Series(dtype=str)).tolist() if str(value).strip()})
    with controls[1]:
        status = st.selectbox("상태", status_options, key=f"{instance}::{context}::status")
    with controls[2]:
        vendor = st.selectbox("주요매입처", vendor_options, key=f"{instance}::{context}::vendor")
    with controls[3]:
        search_text = st.text_input("제품 검색", key=f"{instance}::{context}::search")
    with controls[4]:
        display_limit = st.selectbox("표시 행", [10, 30, 100], index=1, key=f"{instance}::{context}::limit")

    if vendor != "all":
        rows = [
            row for row in rows
            if str(_dashboard_analysis_value(row, "major_purchase_vendor_name")).strip() == vendor
        ]
        frame = _dashboard_analysis_frame(rows, context)
    filtered = frame.copy()
    if status != "전체" and "분석 상태" in filtered:
        filtered = filtered[filtered["분석 상태"].astype(str).eq(status)]
    if str(search_text or "").strip() and not filtered.empty:
        needle = str(search_text).strip().lower()
        filtered = filtered[
            filtered["제품코드"].astype(str).str.lower().str.contains(needle, na=False)
            | filtered["제품명"].astype(str).str.lower().str.contains(needle, na=False)
        ]
    display = filtered.head(int(display_limit)).copy()
    event_match = bool(str(cache.get("dashboard_event_id") or ""))
    log.info(
        "[dashboard.analysis_detail] context=%s selected_status=%s selected_vendor_present=%s search_present=%s "
        "source_rows=%s filtered_rows=%s displayed_rows=%s export_allowed=%s db_query_count=0 event_match=%s room_match=%s",
        context, status, vendor != "all", bool(str(search_text or "").strip()), len(rows), len(filtered), len(display),
        context == "risk", event_match, bool(str(cache.get("room_id") or "")),
    )
    st.caption(f"필터 결과 {len(filtered):,}건 중 상위 {len(display):,}건 표시")
    st.dataframe(display, width="stretch", hide_index=True)


def _render_dashboard_cycle_compact(facts: dict[str, Any], *, side: str) -> None:
    turnover = facts.get("transaction_cycle") or facts.get("turnover_days") or {}
    presentation = _transaction_cycle_presentation(turnover, side=side)
    label = "매입" if side == "purchase" else "매출"
    st.markdown(f"### {label} 거래 주기")
    if presentation.get("message"):
        st.caption(str(presentation["message"]))
        return
    cols = st.columns(4)
    for column, (name, value, suffix) in zip(cols, (
        ("최근 정상 거래일", presentation.get("latest_date") or "-", ""),
        ("경과일", presentation.get("elapsed_days"), "일"),
        ("고유 거래일", presentation.get("unique_trade_days"), "일"),
        ("평균 거래간격", presentation.get("average_interval_days"), "일"),
    )):
        with column:
            _metric_card(name, value if value not in (None, "") else "자료 없음", suffix, digits=1)
    elapsed = presentation.get("elapsed_days")
    interval = presentation.get("average_interval_days")
    if isinstance(elapsed, (int, float)) and isinstance(interval, (int, float)) and interval > 0:
        ratio = min(100.0, max(0.0, float(elapsed) / float(interval) * 100.0))
        st.progress(ratio / 100.0, text=f"최근 거래 이후 경과 {ratio:.0f}%")


def _render_dashboard_facts_v2(facts: dict[str, Any], cache: dict[str, Any], *, render_mode: str) -> None:
    """Preview-only layout that reuses already-built Dashboard facts and charts."""
    inventory = facts.get("inventory") or {}
    sales = facts.get("sales") or {}
    sales_state = _sales_presentation_state(facts)
    inventory_summary = _stock_risk_display_summary(facts)
    amount_unit = _facts_amount_display_unit(facts)
    st.markdown("### 일일 핵심 요약")
    kpis = st.columns(5)
    kpi_values = (
        ("현재매출", sales_state.get("current_sales"), "", amount_unit),
        ("월말 예상매출", sales_state.get("forecast_sales"), "", amount_unit),
        ("매출 진척률", sales_state.get("sales_progress_pct"), "%", ""),
        ("긴급 부족 품목", inventory_summary.get("긴급 부족", {}).get("count", 0), "건", ""),
        ("위험 부족금액", inventory_summary.get("긴급 부족", {}).get("amount", 0), "", amount_unit),
    )
    for column, (label, value, suffix, unit) in zip(kpis, kpi_values):
        with column:
            _metric_card(label, value, suffix, amount_unit=unit)
    phase2 = inventory.get("visual_phase2_summary") or {}
    st.caption(
        " · ".join((
            f"수요 변화 {int(phase2.get('demand_surge_count') or 0):,}건",
            f"입고지연 후보 {int(phase2.get('inbound_delay_candidate_count') or 0):,}건",
            f"재고 없음 {int(phase2.get('cover_zero_stock_count') or 0):,}건",
            f"최근 매입 없음 {int(phase2.get('recent_purchase_none_count') or 0):,}건",
        ))
    )
    briefing_lines = [str(line) for line in phase2.get("briefing_lines") or [] if str(line).strip()][:5]
    if briefing_lines:
        with st.container(border=True):
            st.markdown("### 오늘의 통합 브리핑")
            for line in briefing_lines:
                st.caption(line)

    sales_tab, risk_tab, followup_tab, demand_tab, detail_tab = st.tabs(
        ["매출 현황", "재고·공급 위험", "재고 후속관리", "수요 변화", "상세 자료"]
    )
    with sales_tab:
        sales_render_namespace = _dashboard_render_namespace(cache, render_mode=f"{render_mode}-v2")
        sales_gauge_col, sales_chart_col = st.columns([35, 65])
        with sales_gauge_col:
            _render_sales_gauge(facts, render_namespace=sales_render_namespace)
        with sales_chart_col:
            _render_sales_chart(facts)
        _render_sales_brief(facts)
    with risk_tab:
        summary_col, chart_col = st.columns([35, 65])
        with summary_col:
            _render_stock_risk_summary(facts)
        with chart_col:
            _render_stock_chart(facts)
        _render_vendor_stock_risk(facts)
        if render_mode == "primary":
            st.button("위험 품목 보기", key=f"{_dashboard_analysis_detail_key(cache)}::open-risk", on_click=_set_dashboard_analysis_detail_context, args=(cache, "risk"))
    with followup_tab:
        _render_visual_phase2(facts)
        if render_mode == "primary":
            st.button("재고 후속관리 품목 보기", key=f"{_dashboard_analysis_detail_key(cache)}::open-followup", on_click=_set_dashboard_analysis_detail_context, args=(cache, "followup"))
    with demand_tab:
        _render_demand_surge_detail_summary(facts)
        if render_mode == "primary":
            st.button("수요 변화 품목 보기", key=f"{_dashboard_analysis_detail_key(cache)}::open-demand", on_click=_set_dashboard_analysis_detail_context, args=(cache, "demand"))
    with detail_tab:
        with st.expander("상세 자료 및 다운로드", expanded=False):
            _render_dashboard_analysis_detail(facts, cache, render_mode=render_mode)
            _render_inventory_status_detail(facts, cache, render_mode=render_mode)
    for section, rows in (("risk", len(inventory.get("risk_targets") or [])), ("followup", len(inventory.get("readiness_rows") or [])), ("demand", len(_dashboard_analysis_rows(facts, "demand")))):
        log.info("[dashboard.layout_section] layout_mode=v2 section=%s compact_mode=%s rendered=True source_rows=%s result_rows=%s elapsed_ms=0", section, render_mode != "primary", rows, rows)


def _render_dashboard_facts(
    facts: dict[str, Any],
    cache: dict[str, Any] | None = None,
    *,
    render_mode: str,
) -> None:
    cache = cache or {}
    if _dashboard_layout_v2_enabled(cache):
        _render_dashboard_facts_v2(facts, cache, render_mode=render_mode)
        return
    filter_issues = [
        item
        for item in (facts.get("data_quality") or [])
        if isinstance(item, dict) and item.get("filter_basis") == "not_applied"
    ]
    if filter_issues:
        labels = ", ".join(str(item.get("label") or "제품 조건") for item in filter_issues)
        st.warning(f"{labels} 제외 조건에 필요한 코드 컬럼이 없어 이번 결과에는 적용하지 않았습니다.")
    render_namespace = _dashboard_render_namespace(cache, render_mode=render_mode)
    with st.container(key=f"dashboard_sales_progress__{render_namespace}"):
        st.markdown("## 매출 진행")
        _render_status_cards(facts)
        sales_gauge_col, sales_chart_col = st.columns([3, 7], gap="medium", vertical_alignment="top")
        with sales_gauge_col:
            with st.container(key=f"dashboard_sales_gauge_card__{render_namespace}"):
                _render_sales_gauge(facts, render_namespace=render_namespace)
        with sales_chart_col:
            with st.container(key=f"dashboard_sales_chart_card__{render_namespace}"):
                _render_sales_chart(facts)
        _render_sales_brief(facts)

    with st.container(key=f"dashboard_inventory_summary__{render_namespace}"):
        inventory = facts.get("inventory") or {}
        status_summary = _inventory_status_summary(facts)
        st.markdown("## 재고 현황")
        st.caption(
            f"관리 품목 {int(status_summary.get('total_product_count') or 0):,}개 / "
            f"평가월 예상수요 품목 {int(status_summary.get('expected_demand_product_count') or 0):,}개. "
            "상태는 현재고 ÷ 평가월 예상수요의 비제한 비율로 분류합니다."
        )
        with st.container(key=f"dashboard_inventory_row__top__{render_namespace}"):
            top_left, top_right = st.columns(2, gap="medium", vertical_alignment="top")
            with top_left:
                with st.container(key=f"dashboard_inventory_card__status__{render_namespace}"):
                    _render_summary_card_heading("box", "inventory", "재고 핵심상태", "현재고 ÷ 평가월 예상수요")
                    with st.container(key=f"dashboard_inventory_card_body__status__{render_namespace}"):
                        donut_col, legend_col = st.columns([45, 55], gap="small", vertical_alignment="center")
                        with donut_col:
                            chart = _build_inventory_status_summary_chart(facts)
                            if chart is not None:
                                st.altair_chart(chart, width="stretch")
                        with legend_col:
                            _render_inventory_summary_legend(
                                _inventory_status_rows(facts),
                                total=int(status_summary.get("total_product_count") or 0),
                            )
            with top_right:
                with st.container(key=f"dashboard_inventory_card__frequency__{render_namespace}"):
                    _render_summary_card_heading("cycle", "frequency", "출고빈도 분포", "승인된 직전 완료 3개월 snapshot · A → E / X")
                    with st.container(key=f"dashboard_inventory_card_body__frequency__{render_namespace}"):
                        frequency_chart = _build_outbound_frequency_distribution_chart(facts)
                        if frequency_chart is not None:
                            st.altair_chart(frequency_chart, width="stretch")
                    snapshot_status = str(status_summary.get("snapshot_status") or "missing")
                    if snapshot_status == "ready":
                        st.markdown(
                            '<div class="dashboard-lite-inventory-card-footer">'
                            'X: 최근 3개월 정상 출고 발생 없음. 막대 길이는 최대 등급 대비이며, 화면 필터는 등급 산정 후 적용됩니다.'
                            '</div>',
                            unsafe_allow_html=True,
                        )
                    else:
                        st.warning("출고빈도 자료 부족: 승인·일치하는 snapshot이 없어 등급을 표시하지 않습니다. ERP 실시간 재계산은 수행하지 않습니다.")

        with st.container(key=f"dashboard_inventory_row__bottom__{render_namespace}"):
            bottom_left, bottom_right = st.columns(2, gap="medium", vertical_alignment="top")
            with bottom_left:
                with st.container(key=f"dashboard_inventory_card__surge__{render_namespace}"):
                    _render_demand_surge_summary_card(facts, render_namespace=render_namespace)
            with bottom_right:
                with st.container(key=f"dashboard_inventory_card__vendor__{render_namespace}"):
                    _render_vendor_stock_risk_summary_card(facts, render_namespace=render_namespace)

        _render_inventory_status_detail(facts, cache or {}, render_mode=render_mode)

def _render_dashboard_result_in_primary_area(cache: dict[str, Any]) -> None:
    """Render the active result inline at its chat-history message position."""
    _render_dashboard_result_header(cache)
    _render_dashboard_layout_preview_toggle(cache, render_mode="primary")
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
    _render_dashboard_layout_preview_toggle(cache, render_mode=render_mode)
    _render_dashboard_facts(dict(cache.get("facts") or {}), cache, render_mode="chat")
    return True


def build_dashboard_lite_result_payload(
    params: dict[str, Any],
    *,
    room_id: str,
    company_id: str = "",
    cache_key: str = "",
    action: str = "Dashboard Lite v0.1",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one Dashboard result through the same facts/snapshot contract as the panel."""
    work_params = dict(params or {})
    if company_id:
        work_params["company_id"] = str(company_id)
    started = time.perf_counter()
    facts = build_dashboard_lite_facts(work_params)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    requested_amount_unit = str(work_params.get("amount_display_unit") or "auto").strip().lower()
    resolved_amount_unit = _resolved_dashboard_amount_unit(facts, requested_amount_unit)
    work_params["amount_display_unit_requested"] = requested_amount_unit
    work_params["amount_display_unit_resolved"] = resolved_amount_unit
    facts_filters = dict(facts.get("filters") or {})
    facts_filters["amount_display_unit_requested"] = requested_amount_unit
    facts_filters["amount_display_unit_resolved"] = resolved_amount_unit
    facts_filters["amount_display_unit"] = resolved_amount_unit
    facts["filters"] = facts_filters

    event_id = str(uuid.uuid4())
    result_cache = {
        "cache_key": cache_key,
        "query_fingerprint": _dashboard_cache_key(work_params, run_seq=0),
        "company_id": str(company_id or work_params.get("company_id") or ""),
        "room_id": str(room_id or ""),
        "params": work_params,
        "facts": facts,
        "elapsed_ms": elapsed_ms,
        "elapsed_seconds": round(elapsed_ms / 1000.0, 3),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "dashboard_event_id": event_id,
    }
    for today_action in facts.get("today_actions") or []:
        if isinstance(today_action, dict):
            today_action["source_dashboard_event_id"] = event_id
    payload = {
        "id": event_id,
        "final": True,
        "type": "dashboard_lite",
        "title": "SIMS 일일점검" if action == "SIMS 일일점검" else "Dashboard Lite",
        "action": action,
        "params": dict(work_params),
        "data": None,
        "meta": {
            "analysis_type": "dashboard_lite",
            "facts_kind": facts.get("kind"),
            "room_id": result_cache["room_id"],
            "dashboard_event_id": event_id,
            "dashboard_cache": build_dashboard_lite_chat_snapshot(result_cache),
            "query_summary": _dashboard_scope_header(work_params),
            "source_call_count": int(facts.get("source_call_count") or 0),
        },
    }
    return payload, result_cache


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
    try:
        with st.spinner("Dashboard 조회 중"):
            payload, result_cache = build_dashboard_lite_result_payload(
                params,
                room_id=get_current_chat_room_id(),
                company_id=identity.get("company_id") or "",
                cache_key=cache_key,
            )
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

    st.session_state["__dashboard_lite_result"] = result_cache
    st.session_state["__dashboard_lite_applied_params"] = dict(result_cache.get("params") or params)
    _mark_dashboard_room_title()
    return payload

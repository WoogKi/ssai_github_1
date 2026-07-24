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
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from app.services.dashboard_lite_facts import (
    build_dashboard_lite_facts,
    default_dashboard_lite_scope,
    normalize_dashboard_lite_params,
)
from app.services.ssai_analysis_profile_service import PROFILE_PERMISSION, load_dashboard_profile, save_dashboard_profile
from app.services import rddbc010_service as C01
from app.db.mssql_client import query_to_df
from app.sims.views.rddbc_io_shared import _load_stock_code_options


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
    "__dashboard_lite_exclude_product_group_list",
    "__dashboard_lite_exclude_product_di_list",
    "__dashboard_lite_exclude_product_class_list",
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
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.session_state["__dashboard_lite_styles_loaded"] = True


def _render_status_cards(facts: dict[str, Any]) -> None:
    _inject_dashboard_lite_styles_once()
    sales_metrics = (facts.get("sales") or {}).get("metrics") or {}
    inventory_metrics = (facts.get("inventory") or {}).get("metrics") or {}
    actions = facts.get("today_actions") or []
    amount_unit = str((facts.get("filters") or {}).get("amount_display_unit") or "auto")

    cards = [
        sales_metrics.get("completed_month_avg_sales"),
        sales_metrics.get("current_month_sales"),
        sales_metrics.get("current_month_forecast_sales"),
        sales_metrics.get("current_month_progress_pct"),
        inventory_metrics.get("ready_sku_count"),
        inventory_metrics.get("shortage_sku_count"),
        inventory_metrics.get("sku_readiness_pct"),
        {
            "label": "오늘 조치 필요 건수",
            "value": len(actions),
            "unit": "건",
            "time_basis": "Dashboard deterministic action rules",
        },
    ]
    if amount_unit == "auto":
        amount_values = [
            metric.get("value")
            for metric in cards
            if isinstance(metric, dict) and str(metric.get("unit") or "") in {"원", "금액"}
        ]
        try:
            max_amount = max(abs(float(value)) for value in amount_values if value is not None)
        except ValueError:
            max_amount = 0.0
        divisor, _label = _amount_display_spec("auto", max_amount)
        amount_unit = "million" if divisor == 1_000_000 else ("thousand" if divisor == 1_000 else "won")

    for row in range(0, len(cards), 4):
        cols = st.columns(4)
        for col, metric in zip(cols, cards[row:row + 4]):
            metric = metric or {}
            unit = str(metric.get("unit") or "")
            suffix = "%" if unit == "%" else ("건" if unit == "건" else ("개" if unit == "개" else ""))
            digits = 1 if unit == "%" else 0
            is_amount = unit in {"원", "금액"}
            with col:
                _metric_card(
                    str(metric.get("label") or "-"),
                    metric.get("value"),
                    suffix,
                    help_text=str(metric.get("time_basis") or ""),
                    digits=digits,
                    amount_unit=amount_unit if is_amount else "",
                )


def _build_sales_bar_chart(facts: dict[str, Any]) -> alt.Chart | alt.LayerChart | None:
    """Build the comparable monthly actual/forecast bar chart without reloading facts."""
    rows = (facts.get("sales") or {}).get("chart_rows") or []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if df.empty or not {"period", "period_sort", "kind", "value"}.issubset(df.columns):
        return None
    df = df.sort_values(["period_sort", "kind"], kind="stable").reset_index(drop=True)
    period_order = (
        df[["period", "period_sort"]]
        .drop_duplicates()
        .sort_values("period_sort", kind="stable")["period"]
        .tolist()
    )

    actual_df = df[~df["kind"].astype(str).str.contains("예상", na=False)].copy()
    forecast_df = df[df["kind"].astype(str).str.contains("예상", na=False)].copy()
    tooltip = [
        alt.Tooltip("period:N", title="기간"),
        alt.Tooltip("kind:N", title="구분"),
        alt.Tooltip("value:Q", title="매출", format=",.0f"),
        alt.Tooltip("partial_period:N", title="부분월"),
        alt.Tooltip("forecast_status:N", title="예상 상태"),
        alt.Tooltip("forecast_basis:N", title="예상 기준"),
    ]
    kind_color = alt.Color(
        "kind:N",
        title="구분",
        scale=alt.Scale(
            domain=["완료월 실제", "당월 현재(부분월)", "완료월 사전예상", "당월 예상"],
            range=["#2563eb", "#2563eb", "#f97316", "#f97316"],
        ),
    )
    x_encoding = alt.X("period:N", title="기간", sort=period_order)
    y_encoding = alt.Y("value:Q", title="매출", stack=None)
    base = alt.Chart(df).encode(
        x=x_encoding,
        y=y_encoding,
        tooltip=tooltip,
    )
    layers = []
    # Forecast is behind the narrower actual bar at the same monthly center point.
    if not forecast_df.empty:
        layers.append(
            alt.Chart(forecast_df).mark_bar(opacity=0.55, size=28).encode(
                x=x_encoding,
                y=y_encoding,
                color=kind_color,
                tooltip=tooltip,
            )
        )
    if not actual_df.empty:
        layers.append(
            alt.Chart(actual_df).mark_bar(opacity=0.88, size=18).encode(
                x=x_encoding,
                y=y_encoding,
                color=kind_color,
                tooltip=tooltip,
            )
        )
    return (
        alt.layer(*layers).resolve_scale(y="shared").properties(height=260)
        if layers
        else base.mark_bar().properties(height=260)
    )


def _render_sales_chart(facts: dict[str, Any]) -> None:
    chart = _build_sales_bar_chart(facts)
    if chart is None:
        st.info("매출 그래프를 표시할 완료월/당월 facts가 없습니다.")
        return
    st.altair_chart(chart, width="stretch")


def _render_stock_chart(facts: dict[str, Any]) -> None:
    rows = (facts.get("inventory") or {}).get("risk_targets") or []
    if not rows:
        st.info("98% 미만 재고준비율 조치 대상이 없습니다.")
        return
    df = pd.DataFrame(rows).head(10).copy()
    threshold = pd.DataFrame({"threshold": [98.0]})
    bars = (
        alt.Chart(df)
        .mark_bar(color="#ef4444")
        .encode(
            x=alt.X("stock_readiness_pct:Q", title="재고준비율(%)", scale=alt.Scale(domain=[0, 100])),
            y=alt.Y("product_name:N", title="제품", sort="-x"),
            tooltip=[
                alt.Tooltip("product_name:N", title="제품"),
                alt.Tooltip("stock_readiness_pct:Q", title="준비율", format=".1f"),
                alt.Tooltip("remaining_expected_demand_qty:Q", title="잔여예상수요", format=",.0f"),
                alt.Tooltip("shortage_qty:Q", title="부족수량", format=",.0f"),
            ],
        )
    )
    rule = alt.Chart(threshold).mark_rule(color="#0f766e", strokeDash=[4, 4]).encode(x="threshold:Q")
    st.altair_chart((bars + rule).properties(height=280), width="stretch")


def _render_turnover(facts: dict[str, Any]) -> None:
    turnover = facts.get("turnover_days") or {}
    st.caption("매입/매출 거래 회전일")
    st.info(
        "v0.1에서는 최근 90일 정상 매입/매출 고유 거래일 facts가 아직 연결되지 않아 자료부족으로 표시합니다. "
        "입금/출금/현금 회전일은 원천자료가 없어 표시하지 않습니다."
    )
    if turnover.get("definition"):
        st.caption(str(turnover.get("definition")))


def _render_today_actions(facts: dict[str, Any]) -> None:
    actions = facts.get("today_actions") or []
    st.markdown("#### 오늘 우선 확인할 제품 10개")
    st.caption(
        "선택한 재고위치와 제품 제외조건을 적용한 결과입니다. "
        "재고준비율 98% 미만 제품 중 부족 영향이 큰 순서로 표시합니다."
    )
    if not actions:
        st.success("현재 기본 규칙으로 조치가 필요한 항목이 없습니다.")
    else:
        display_rows = []
        for action in actions[:10]:
            display_rows.append(
                {
                    "우선순위": int(action.get("rank") or len(display_rows) + 1),
                    "위험등급": action.get("risk_grade") or action.get("priority") or "",
                    "제품코드": action.get("product_code") or "",
                    "제품명": action.get("product_name") or action.get("target") or "",
                    "제약사명": action.get("manufacturer_name") or "",
                    "현재 사용 가능 재고": float(action.get("current_stock_qty") or 0),
                    "당월 잔여예상수요": float(action.get("remaining_expected_demand_qty") or 0),
                    "부족예상수량": float(action.get("shortage_qty") or 0),
                    "부족예상금액": float(action.get("shortage_amt") or 0),
                    "재고준비율": float(action.get("stock_readiness_pct") or 0),
                    "권장조치": action.get("recommended_action") or "",
                    "상세표": action.get("drill_down") or "",
                }
            )
        st.dataframe(
            pd.DataFrame(display_rows),
            width="stretch",
            height=388,
            hide_index=True,
            column_config={
                "우선순위": st.column_config.NumberColumn("우선순위", format="%d"),
                "현재 사용 가능 재고": st.column_config.NumberColumn("현재 사용 가능 재고", format="%,.0f"),
                "당월 잔여예상수요": st.column_config.NumberColumn("당월 잔여예상수요", format="%,.0f"),
                "부족예상수량": st.column_config.NumberColumn("부족예상수량", format="%,.0f"),
                "부족예상금액": st.column_config.NumberColumn("부족예상금액", format="%,.0f"),
                "재고준비율": st.column_config.NumberColumn("재고준비율", format="%.1f%%"),
            },
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
    restored_widget_count = 0
    skipped_existing_widget_count = 0
    if isinstance(profile, dict):
        for source_key, widget_key in _DASHBOARD_PROFILE_WIDGETS.items():
            if widget_key in st.session_state:
                skipped_existing_widget_count += 1
                continue
            if source_key in profile:
                st.session_state[widget_key] = _dashboard_profile_widget_value(source_key, profile[source_key])
                restored_widget_count += 1
        io_values = _clean_list(profile.get("io_gu_list"))
        log.info(
            "[dashboard.profile_restore] company_id=%s reason=%s profile_found=True "
            "condition_keys=%s io_gu_count=%s io_gu_sample=%s restored_widget_count=%s "
            "skipped_existing_widget_count=%s",
            identity["company_id"], restore_reason, ",".join(sorted(profile.keys())), len(io_values), ",".join(io_values[:3]),
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
        "manufacturer_test_codes": _normalized_key_list(params.get("manufacturer_test_codes")),
        "major_purchase_vendor_days": params.get("major_purchase_vendor_days"),
        "risk_analysis_days": params.get("risk_analysis_days"),
        "overstock_inactive_days": params.get("overstock_inactive_days"),
        "readiness_warning_pct": params.get("readiness_warning_pct"),
        "risk_quick_view_count": params.get("risk_quick_view_count"),
        "amount_display_unit": params.get("amount_display_unit"),
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
    with st.form("dashboard_lite_scope_form", clear_on_submit=False):
        cols = st.columns(4)
        with cols[0]:
            month_from = st.text_input("시작월", value=defaults["month_from"], max_chars=6, help="YYYYMM")
        with cols[1]:
            month_to = st.text_input("종료월", value=defaults["month_to"], max_chars=6, help="YYYYMM")
        with cols[2]:
            evaluation_month = st.text_input("평가월", value=defaults["evaluation_month"], max_chars=6, help="YYYYMM")
        with cols[3]:
            manufacturer_text = st.text_input("제약사", key="__dashboard_lite_manufacturer_text")
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
                io_gu = st.multiselect("입출고구분", options=io_gu_codes, key="__dashboard_lite_io_gu_list", format_func=lambda code: _option_label(code, io_gu_code_to_name.get(str(code), "")), help="미선택 시 0012 업무코드 전체를 사용합니다.")

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
    manufacturer_result = {"status": "all", "codes": []}
    if submitted:
        manufacturer_result = _resolve_dashboard_manufacturer(manufacturer_text)
        if manufacturer_result.get("status") == "too_short":
            st.warning("제약사명 검색은 두 글자 이상 입력해 주세요.")
            return False, False, None
        if manufacturer_result.get("status") in {"missing", "error"}:
            st.warning("해당 제약사를 찾을 수 없습니다.")
            return False, False, None
    elif not str(manufacturer_text or "").strip() or str(manufacturer_text or "").strip() == "전체":
        _clear_dashboard_manufacturer_state(keep_text=True)
    stock_cd_list = _clean_list(selected_stock_labels)
    stock_name_list = [stock_code_to_name.get(code, "") for code in stock_cd_list]
    product_group_codes_selected = _clean_list(product_groups)
    product_di_codes_selected = _clean_list(product_di)
    product_class_codes_selected = _clean_list(product_class)
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
        "manufacturer_test_codes": _clean_list(st.session_state.get("__dashboard_lite_manufacturer_test_codes")),
        "manufacturer_scope_label": str((st.session_state.get("__dashboard_lite_manufacturer_scope") or {}).get("label") or "전체"),
        "manufacturer_search_term": str((st.session_state.get("__dashboard_lite_manufacturer_scope") or {}).get("search_term") or ""),
        "manufacturer_match_mode": str((st.session_state.get("__dashboard_lite_manufacturer_scope") or {}).get("match_mode") or "all"),
        "manufacturer_match_count": int((st.session_state.get("__dashboard_lite_manufacturer_scope") or {}).get("match_count") or 0),
        "manufacturer_names": _clean_list((st.session_state.get("__dashboard_lite_manufacturer_scope") or {}).get("names")),
        "major_purchase_vendor_days": major_purchase_vendor_days,
        "risk_analysis_days": risk_analysis_days,
        "overstock_inactive_days": overstock_inactive_days,
        "readiness_warning_pct": readiness_warning_pct,
        "risk_quick_view_count": risk_quick_view_count,
        "amount_display_unit": amount_display_unit,
    }
    try:
        params = normalize_dashboard_lite_params(raw_params, today=date.today())
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
    manufacturer_label = str(params.get("manufacturer_scope_label") or "전체").strip() or "전체"
    parts.append(f"제약사: {manufacturer_label}")
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
            "manufacturer_scope_label", "manufacturer_search_term", "manufacturer_match_mode", "manufacturer_match_count",
        )
        if key in params
    }
    compact_params["manufacturer_scope_label"] = str(compact_params.get("manufacturer_scope_label") or "전체")
    compact_params["manufacturer_codes"] = _clean_list(params.get("manufacturer_test_codes"))[:row_limit]
    compact_params["manufacturer_names"] = _clean_list(params.get("manufacturer_names"))[:row_limit]
    compact_facts = {
        "kind": facts.get("kind"),
        "period": facts.get("period"),
        "sales": {
            "metrics": dict(sales.get("metrics") or {}),
            "chart_rows": list(sales.get("chart_rows") or []),
            "decline_targets": list(sales.get("decline_targets") or [])[:row_limit],
        },
        "inventory": {
            "metrics": dict(inventory.get("metrics") or {}),
            "risk_targets": list(inventory.get("risk_targets") or [])[:row_limit],
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
        "params": compact_params,
        "facts": compact_facts,
        "elapsed_ms": source.get("elapsed_ms"),
        "elapsed_seconds": source.get("elapsed_seconds"),
        "created_at": source.get("created_at"),
    }


def _mark_dashboard_room_title() -> bool:
    """Name only a still-empty auto-created room; never replace a normal query title."""
    ss = st.session_state
    room_id = str(ss.get("__chat_current_room_id") or "").strip()
    if not room_id:
        return False
    rooms = ss.get("chat_rooms") or []
    for room in rooms:
        if not isinstance(room, dict) or str(room.get("id") or "") != room_id:
            continue
        has_messages = any(bool(room.get(key)) for key in ("messages", "history", "sims_messages", "gen_messages"))
        if room.get("auto_created") is not True or has_messages:
            return False
        room["name"] = "Dashboard Lite"
        room["auto_created"] = False
        room["name_auto"] = False
        room["title_initialized"] = True
        room["title_source"] = "dashboard_lite"
        room["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ss["__chat_room_title_changed_room_id"] = room_id
        ss["__chat_room_title_changed_name"] = "Dashboard Lite"
        log.info("[dashboard.room_title] room_title_set=True")
        return True
    return False


def _render_dashboard_result_header(cache: dict[str, Any]) -> None:
    params = dict(cache.get("params") or {})
    elapsed_ms = int(cache.get("elapsed_ms") or 0)
    created_at = str(cache.get("created_at") or "").strip()
    st.markdown("## Dashboard Lite")
    st.caption(_dashboard_scope_header(params))
    if created_at:
        st.caption(f"조회 완료 · {max(0, elapsed_ms) / 1000:.1f}초 · {created_at}")
    else:
        st.caption(f"조회 완료 · {max(0, elapsed_ms) / 1000:.1f}초")


def _render_dashboard_facts(facts: dict[str, Any]) -> None:
    filter_issues = [
        item
        for item in (facts.get("data_quality") or [])
        if isinstance(item, dict) and item.get("filter_basis") == "not_applied"
    ]
    if filter_issues:
        labels = ", ".join(str(item.get("label") or "제품 조건") for item in filter_issues)
        st.warning(f"{labels} 제외 조건에 필요한 코드 컬럼이 없어 이번 결과에는 적용하지 않았습니다.")
    _render_status_cards(facts)
    st.divider()

    st.markdown("### 매출 그래프")
    st.caption("완료월 실제값과 당시 시점의 사전예상, 당월 부분월 현재/예상값을 같은 월 기준으로 비교합니다.")
    _render_sales_chart(facts)

    st.markdown("### 재고 그래프")
    st.caption("98% 미만 SKU만 기본 조치 대상으로 표시합니다.")
    _render_stock_chart(facts)

    st.markdown("### 매입·매출 거래 회전일")
    _render_turnover(facts)

    st.markdown("### 오늘의 조치")
    _render_today_actions(facts)

def _render_dashboard_result_in_primary_area(cache: dict[str, Any]) -> None:
    target = _DASHBOARD_RENDER_TARGET
    if target is not None and hasattr(target, "container"):
        with target.container():
            _render_dashboard_result_header(cache)
            _render_dashboard_facts(dict(cache.get("facts") or {}))
        return
    _render_dashboard_result_header(cache)
    _render_dashboard_facts(dict(cache.get("facts") or {}))


def render_cached_dashboard_lite_primary() -> bool:
    """Re-render cached facts in the main result area without any source reload."""
    cache = st.session_state.get("__dashboard_lite_result")
    facts = cache.get("facts") if isinstance(cache, dict) else None
    if not isinstance(facts, dict) or not facts:
        return False
    _render_dashboard_result_in_primary_area(cache)
    st.session_state["__dashboard_lite_primary_rendered_this_run"] = True
    return True


def render_dashboard_lite_chat_item(cache: dict[str, Any]) -> None:
    """Render one immutable Dashboard result inside its chat history message."""
    _render_dashboard_result_header(cache)
    _render_dashboard_facts(dict(cache.get("facts") or {}))


def render_dashboard_lite() -> dict[str, Any]:
    """Render Dashboard Lite without changing current-table routing."""
    st.subheader("Dashboard Lite v0.1")
    st.caption("상태 → 근거 → 무엇을 해야 하나 순서로 읽는 운영 브리핑입니다.")

    _apply_saved_dashboard_profile_once()
    submitted, save_requested, params = _render_dashboard_scope_form()
    run_seq = int(st.session_state.get("__dashboard_lite_run_seq") or 0)
    cache = st.session_state.get("__dashboard_lite_result")

    if submitted:
        st.session_state["__dashboard_lite_run_seq"] = run_seq + 1
        run_seq += 1
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
    st.session_state["__dashboard_lite_result"] = {
        "cache_key": cache_key,
        "query_fingerprint": _dashboard_cache_key(params, run_seq=0),
        "company_id": identity.get("company_id") or "",
        "params": params,
        "facts": facts,
        "elapsed_ms": elapsed_ms,
        "elapsed_seconds": round(elapsed_ms / 1000.0, 3),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    _mark_dashboard_room_title()
    return {
        "final": True,
        "type": "dashboard_lite",
        "title": "Dashboard Lite",
        "action": "Dashboard Lite v0.1",
        "params": dict(params),
        "data": None,
        "meta": {
            "analysis_type": "dashboard_lite",
            "facts_kind": facts.get("kind"),
            "dashboard_cache": build_dashboard_lite_chat_snapshot(st.session_state["__dashboard_lite_result"]),
            "query_summary": _dashboard_scope_header(params),
        },
    }

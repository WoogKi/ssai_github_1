"""Focused gate for Dashboard current-month business-day progress."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.business_calendar_service import BusinessDayMonthContext, business_day_month_context
from app.services.dashboard_inventory_frequency_snapshot import FrequencyProjectionReadResult
from app.services.dashboard_lite_facts import (
    _apply_current_month_demand_surge,
    _build_sales_facts,
    build_dashboard_lite_facts,
)
from app.services.ssai_business_calendar_repository import CalendarAuthorityRead


def _ready_context() -> BusinessDayMonthContext:
    calls = {"count": 0}

    def _calendar_loader(**_kwargs) -> CalendarAuthorityRead:
        calls["count"] += 1
        return CalendarAuthorityRead(
            status="ready",
            holiday_dates=frozenset({"20260924", "20260925"}),
        )

    context = business_day_month_context(
        evaluation_date=date(2026, 9, 11),
        calendar_loader=_calendar_loader,
    )
    assert calls["count"] == 1
    assert context.calendar_days_total == 30
    assert context.elapsed_calendar_days == 11
    assert abs(context.calendar_progress_ratio - 11 / 30) < 1e-12
    assert context.business_days_total == 20
    assert context.elapsed_business_days == 9
    assert abs(float(context.business_day_progress_ratio) - 9 / 20) < 1e-12
    return context


def _sales_payload() -> dict:
    return {
        "df": pd.DataFrame([{
            "제약사명": "fixture",
            "완료월총매출": 600.0,
            "완료월수": 6,
            "당월 현재매출": 50.0,
            "당월 예상매출": 100.0,
        }]),
        "meta": {"evaluation_month": "202609"},
    }


def main() -> int:
    context = _ready_context()
    sales = _build_sales_facts(
        _sales_payload(),
        evaluation_month="202609",
        policy_date="20260911",
        today=date(2026, 9, 11),
        business_day_context=context,
    )
    visualization = sales["visualization"]
    assert abs(float(visualization["time_progress_pct"]) - 45.0) < 1e-12
    assert abs(float(visualization["expected_to_date_sales"]) - 45.0) < 1e-12
    assert abs(float(visualization["time_adjusted_achievement_pct"]) - (50 / 45 * 100)) < 1e-12
    assert float(visualization["forecast_sales"]) == 100.0

    rows = [{
        "당월현재출고수량": 45.0,
        "당월기준예상출고수량": 40.0,
        "remaining_expected_demand_qty": 5.0,
        "current_stock_qty": 10.0,
        "stock_valuation_unit_price": 1.0,
    }]
    surge = _apply_current_month_demand_surge(
        rows,
        evaluation_month="202609",
        policy_date="20260911",
        business_day_context=context,
    )
    assert rows[0]["수요급증여부"] is True
    assert rows[0]["진행속도기준월말예상출고수량"] == 100.0
    assert rows[0]["위험보정기준"] == "영업일 진행속도 보정"
    assert surge["evaluation_elapsed_days"] == 9
    assert surge["evaluation_total_days"] == 20
    assert rows[0]["평가월잔여일수"] == 19

    unavailable = business_day_month_context(
        evaluation_date=date(2026, 9, 11),
        calendar_loader=lambda **_kwargs: CalendarAuthorityRead(
            status="unavailable",
            reason_code="calendar_year_not_loaded",
        ),
    )
    unavailable_sales = _build_sales_facts(
        _sales_payload(),
        evaluation_month="202609",
        policy_date="20260911",
        today=date(2026, 9, 11),
        business_day_context=unavailable,
    )
    unavailable_visualization = unavailable_sales["visualization"]
    assert unavailable_visualization["expected_to_date_sales"] is None
    assert unavailable_visualization["time_adjusted_achievement_pct"] is None
    assert unavailable_visualization["forecast_sales"] == 100.0
    unavailable_rows = [dict(rows[0], 수요급증여부=False)]
    _apply_current_month_demand_surge(
        unavailable_rows,
        evaluation_month="202609",
        policy_date="20260911",
        business_day_context=unavailable,
    )
    assert unavailable_rows[0]["수요급증여부"] is False
    assert unavailable_rows[0]["진행속도기준월말예상출고수량"] is None
    assert unavailable_rows[0]["위험보정기준"] == "영업일자료부족"

    zero_elapsed = BusinessDayMonthContext(
        evaluation_date="20260901",
        evaluation_month="202609",
        calendar_days_total=30,
        elapsed_calendar_days=1,
        calendar_progress_ratio=1 / 30,
        business_days_total=20,
        elapsed_business_days=0,
        business_day_progress_ratio=0.0,
        authority_status="ready",
    )
    zero_rows = [dict(rows[0], 수요급증여부=False)]
    _apply_current_month_demand_surge(
        zero_rows,
        evaluation_month="202609",
        policy_date="20260901",
        business_day_context=zero_elapsed,
    )
    assert zero_rows[0]["수요급증여부"] is False
    assert zero_rows[0]["진행속도기준월말예상출고수량"] is None
    assert zero_rows[0]["위험보정기준"] == "영업일진행률계산불가"

    assembly_calls = {"count": 0}

    def _context_loader(**_kwargs) -> BusinessDayMonthContext:
        assembly_calls["count"] += 1
        return context

    facts = build_dashboard_lite_facts(
        {
            "month_from": "202603",
            "month_to": "202608",
            "evaluation_month": "202609",
            "policy_date": "20260911",
        },
        manufacturer_summary_payload=_sales_payload(),
        stock_shortage_payload={"df": pd.DataFrame(), "meta": {}},
        inbound_facts_df=pd.DataFrame(),
        frequency_projection_reader=lambda **_kwargs: FrequencyProjectionReadResult(
            status="missing",
            reason="fixture",
        ),
        today=date(2026, 9, 11),
        business_day_context_loader=_context_loader,
    )
    assert assembly_calls["count"] == 1
    assert facts["business_day_month_context"]["business_day_progress_ratio"] == 9 / 20
    assert facts["source_call_count"] == 0
    assert facts["sales"]["visualization"]["expected_to_date_sales"] == 45.0

    print("PASS dashboard business-day progress: calendar=11/30 business=9/20 expected=45% pace=20/9")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

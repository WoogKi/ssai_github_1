"""Offline contract gate for Dashboard marginal totals, risk bands, and sales remaining."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_lite_facts import (
    _attach_inventory_status_and_frequency, _build_sales_facts, _classify_stock_risk_rows,
)
from app.services.dashboard_inventory_frequency_snapshot import FrequencyProjectionReadResult
from app.services.ssai_snapshot_repository import SnapshotReadResult
from app.sims.views import dashboard_lite as view


def _matrix_from_margins(row_totals: list[int], column_totals: list[int]) -> list[list[int]]:
    remaining = column_totals.copy()
    matrix = []
    for target in row_totals:
        row = []
        for index in range(len(remaining)):
            value = min(target, remaining[index])
            row.append(value)
            target -= value
            remaining[index] -= value
        assert target == 0
        matrix.append(row)
    assert not any(remaining)
    return matrix


def _risk_row(code: str, stock: float, demand: float = 100.0, *, present: bool = True) -> dict:
    return {
        "product_code": code, "current_stock_qty": stock, "current_stock_amt": stock,
        "remaining_expected_demand_qty": demand, "shortage_qty": max(demand - stock, 0),
        "shortage_amt": max(demand - stock, 0), "stock_readiness_pct": stock,
        "stock_valuation_unit_price": 1, "_stock_risk_required_values_present": present,
    }


def main() -> int:
    row_totals = [173, 19, 32, 1807, 461, 2927, 1282, 3384]
    column_totals = [299, 4, 60, 4135, 2285, 3293, 9]
    assert sum(row_totals) == sum(column_totals) == 10085
    matrix = _matrix_from_margins(row_totals, column_totals)
    # The supplied company-4 fixture has seven ongoing statuses (no completed-month state).
    cross_rows = [
        {"재고상태": status, "월간 소진상태": depletion, "품목수": matrix[i][j]}
        for i, status in enumerate(view.INVENTORY_STATUS_ORDER[1:])
        for j, depletion in enumerate((
            "월예상 초과 달성", "월예상 달성", "예상외 출고", "재고로 월말 충족 가능",
            "입고예정 반영 시 충족", "월말 부족 예상", "월예상 수요 없음",
        ))
    ]
    cross_facts = {"inventory": {"monthly_depletion_summary": {
        "inventory_status_cross_rows": cross_rows, "total_product_count": 10085,
        "status_counts": {name: total for name, total in zip(
            ("월예상 초과 달성", "월예상 달성", "예상외 출고", "재고로 월말 충족 가능",
             "입고예정 반영 시 충족", "월말 부족 예상", "월예상 수요 없음"), column_totals,
        )},
    }}}
    cross = view._monthly_depletion_cross_table(cross_facts)
    assert cross.loc["합계", "합계"] == 10085
    assert cross.loc["합계"].drop("합계").tolist() == column_totals
    assert cross["합계"].drop("합계").tolist() == row_totals
    assert sum(row["count"] for row in view._monthly_depletion_rows(cross_facts)) == 10085
    rendered: list[str] = []
    captured_tables: list[pd.io.formats.style.Styler] = []
    fake_st = SimpleNamespace(markdown=lambda value, **_: rendered.append(value),
                              caption=lambda value: None, dataframe=lambda value, **_: captured_tables.append(value))
    with patch.object(view, "st", fake_st):
        view._render_monthly_depletion_summary(cross_facts)
        view._render_monthly_depletion_cross_distribution(cross_facts)
    assert sum("height:36px" in value for value in rendered) == 1
    assert "height:18px" not in "".join(rendered)
    assert len(captured_tables) == 1 and "#c8d9e9" in captured_tables[0].to_html()

    grade_totals = [435, 396, 1999, 4068, 1816, 390]
    contribution_totals = [1331, 998, 1042, 2107, 3147, 479]
    assert sum(grade_totals) == sum(contribution_totals) == 9104
    grades = "ABCDEX"
    grade_cells = _matrix_from_margins(grade_totals, contribution_totals)
    grade_matrix = {profit: dict(zip(grades, grade_cells[i])) for i, profit in enumerate(grades)}
    summary = {"profit_contribution_grade_matrix": grade_matrix, "total_product_count": 10085}
    heat_rows = view._profit_contribution_heatmap_rows(summary)
    body = [row for row in heat_rows if not row["is_total"]]
    margins = [row for row in heat_rows if row["is_total"]]
    assert len(body) == 36 and len(margins) == 13
    assert [row["count"] for row in margins[:6]] == grade_totals
    assert [row["count"] for row in margins[6:12]] == contribution_totals
    assert margins[-1]["count"] == 9104 and 9104 + 981 == 10085
    assert all(row["visual_intensity"] is None for row in margins)
    chart = view._build_profit_contribution_grade_heatmap({"inventory": {"inventory_status_summary": summary}})
    assert chart is not None and len(chart.layer[0].data) == 36 and len(chart.layer[1].data) == 13
    assert chart.to_dict()["layer"][0]["encoding"]["color"]["scale"]["domain"] == [0, 0.2, 0.4, 0.6, 0.8, 1]

    risk_rows = [_risk_row("49.99", 49.99), _risk_row("50", 50),
                 _risk_row("99.99", 99.99), _risk_row("100", 100),
                 _risk_row("no-demand", 1, 0), _risk_row("missing", 1, present=False)]
    with patch("app.services.dashboard_lite_facts.log.info") as risk_log:
        _classify_stock_risk_rows(risk_rows, readiness_warning_pct=30)
    risk_message, *risk_args = risk_log.call_args.args
    assert "risk_emergency_pct=50.0 risk_full_pct=100.0 legacy_readiness_warning_pct=30.0" in risk_message % tuple(risk_args)
    with patch("app.services.dashboard_lite_facts.log.info") as empty_risk_log:
        _classify_stock_risk_rows([], readiness_warning_pct=30)
    empty_message, *empty_args = empty_risk_log.call_args.args
    assert "risk_emergency_pct=50.0 risk_full_pct=100.0 legacy_readiness_warning_pct=30.0" in empty_message % tuple(empty_args)
    assert [row["재고위험상태"] for row in risk_rows] == [
        "긴급 부족", "부족 주의", "부족 주의", "적정", "판정 제외", "판정 제외",
    ]
    surge = _risk_row("surge", 60)
    surge.update({"수요급증여부": True, "위험보정잔여예상수요": 100, "위험보정재고준비율": 60})
    surge_rows = [surge]
    _classify_stock_risk_rows(surge_rows, readiness_warning_pct=30)
    assert surge_rows[0]["재고위험상태"] == "부족 주의"
    assert view._dashboard_readiness_threshold({}, {"readiness_warning_pct": 30}) == 50

    inventory_rows = [
        {"product_code": str(stock), "product_name": str(stock), "current_stock_qty": stock,
         "inventory_current_stock_present": True, "evaluation_expected_demand_qty": 100,
         "evaluation_expected_demand_present": True, "raw_pending_inbound_qty": 0}
        for stock in (4.99, 5, 15, 75, 110)
    ]
    _attach_inventory_status_and_frequency(
        inventory_rows, frequency_snapshot=SnapshotReadResult(status="missing", reason="fixture"),
        previous_frequency_projection=FrequencyProjectionReadResult(status="not_requested", reason="fixture"),
        monthly_business_days=20, pending_available=False,
    )
    assert [row["inventory_status"] for row in inventory_rows] == [
        "긴급 부족", "재고 부족", "안전재고 확보", "적정 재고", "과다 재고",
    ]

    sales_frame = pd.DataFrame([
        {"당월 예상매출": 100, "당월 현재매출": 120, "당월 잔여예상": 0},
        {"당월 예상매출": 100, "당월 현재매출": 50, "당월 잔여예상": 50},
    ])
    def sales(frame: pd.DataFrame, month: str = "202610", today: date = date(2026, 10, 3)) -> dict:
        return _build_sales_facts({"df": frame, "meta": {"evaluation_month": month}},
                                  evaluation_month=month, policy_date="20261003", today=today)
    result = sales(sales_frame)
    assert sales_frame["당월 잔여예상"].sum() == 50
    assert result["metrics"]["current_month_remaining_forecast_sales"]["value"] == 30
    assert result["visualization"]["remaining_forecast"] == 30
    facts = {"sales": result}
    state = view._sales_presentation_state(facts)
    assert state["remaining_forecast_sales"] == state["comparison_amount"] == 30
    assert sales(pd.DataFrame([{"당월 예상매출": 100, "당월 현재매출": 120, "당월 잔여예상": 0}]))["visualization"]["remaining_forecast"] == 0
    company4 = sales(pd.DataFrame([{"당월 예상매출": 27293646143, "당월 현재매출": 4540655284}]))
    assert company4["visualization"]["remaining_forecast"] == 22752990859
    completed_frame = sales_frame.assign(**{"평가월 예상매출": [100, 100]})
    completed = sales(completed_frame, month="202609")
    assert completed["metrics"]["current_month_remaining_forecast_sales"]["value"] == 0
    print("PASS dashboard final semantic UI: cross=10085 grades=9104 missing=981 risk=50/100 remaining=30")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

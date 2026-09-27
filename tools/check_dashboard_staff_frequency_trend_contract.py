from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import FrequencyProjectionReadResult
from app.services.dashboard_lite_facts import (
    _attach_inventory_status_and_frequency,
    dashboard_staff_filter_active,
    filter_dashboard_inbound_facts_by_staff,
)
from app.services.ssai_snapshot_repository import SnapshotReadResult
from app.sims.views.dashboard_lite import _dashboard_grade_distribution_rows, _build_dashboard_grade_distribution_chart


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _staff_fixture() -> None:
    frame = pd.DataFrame(
        [
            {"product_code": "P1", "recent_inbound_vendor_staff_code": "O1", "recent_inbound_vendor_staff_name": "김발주", "manufacturer_staff_code": "M1", "manufacturer_staff_name": "이제약"},
            {"product_code": "P2", "recent_inbound_vendor_staff_code": "O1", "recent_inbound_vendor_staff_name": "김발주", "manufacturer_staff_code": "M2", "manufacturer_staff_name": "박제약"},
            {"product_code": "P3", "recent_inbound_vendor_staff_code": "O2", "recent_inbound_vendor_staff_name": "윤발주", "manufacturer_staff_code": "M1", "manufacturer_staff_name": "이제약"},
        ]
    )
    exact = filter_dashboard_inbound_facts_by_staff(frame, {"dashboard_order_staff_codes": ["O1"]})
    _assert(exact["product_code"].tolist() == ["P1", "P2"], "order staff exact filter failed")
    partial = filter_dashboard_inbound_facts_by_staff(frame, {"dashboard_pharma_staff_nm": "이"})
    _assert(partial["product_code"].tolist() == ["P1", "P3"], "pharma staff partial filter failed")
    combined = filter_dashboard_inbound_facts_by_staff(
        frame,
        {"dashboard_order_staff_codes": ["O1"], "dashboard_pharma_staff_codes": ["M1"]},
    )
    _assert(combined["product_code"].tolist() == ["P1"], "staff AND filter failed")
    _assert(dashboard_staff_filter_active({"dashboard_order_staff_nm": "김"}), "name filter was not activated")


def _frequency_fixture() -> None:
    rows = [
        {"product_code": "P1", "product_name": "제품1", "inventory_current_stock_present": True, "evaluation_expected_demand_present": True, "current_stock_qty": 20, "evaluation_expected_demand_qty": 100},
        {"product_code": "P2", "product_name": "제품2", "inventory_current_stock_present": True, "evaluation_expected_demand_present": True, "current_stock_qty": 100, "evaluation_expected_demand_qty": 100},
        {"product_code": "P3", "product_name": "제품3", "inventory_current_stock_present": True, "evaluation_expected_demand_present": True, "current_stock_qty": 200, "evaluation_expected_demand_qty": 100},
    ]
    current = (
        {"product_code": "P1", "frequency_grade": "F", "occurrence_count_3m": 0},
        {"product_code": "P2", "frequency_grade": "A", "occurrence_count_3m": 30, "profit_grade": "A", "contribution_grade": "B"},
        {"product_code": "P3", "frequency_grade": "X", "occurrence_count_3m": 0, "profit_grade": "X", "contribution_grade": "X"},
    )
    previous = FrequencyProjectionReadResult(
        status="ready",
        rows=(
            {"product_code": "P1", "frequency_grade": "X"},
            {"product_code": "P2", "frequency_grade": "B"},
            {"product_code": "P3", "frequency_grade": "A"},
        ),
        generation_no=8,
        resolved_evaluation_month="202608",
    )
    result = _attach_inventory_status_and_frequency(
        rows,
        frequency_snapshot=SnapshotReadResult(status="ready", generation_no=9),
        frequency_rows=current,
        frequency_evaluation_month="202609",
        previous_frequency_projection=previous,
        previous_frequency_evaluation_month="202608",
    )
    summary = result["summary"]
    _assert(summary["frequency_counts"]["F"] == 1, "F grade was not retained")
    facts = {"inventory": {"inventory_status_summary": summary}}
    for key, expected_order, expected_counts in (
        ("frequency_counts", ["A", "B", "C", "D", "E", "F", "X", "빈도자료 부족"], {"A": 1, "F": 1, "X": 1}),
        ("profit_grade_counts", ["A", "B", "C", "D", "E", "X", "등급자료 부족"], {"A": 1, "X": 1, "등급자료 부족": 1}),
        ("contribution_grade_counts", ["A", "B", "C", "D", "E", "X", "등급자료 부족"], {"B": 1, "X": 1, "등급자료 부족": 1}),
    ):
        distribution = _dashboard_grade_distribution_rows(facts, key)
        _assert([item["grade"] for item in distribution] == expected_order, f"{key} order mismatch")
        _assert(sum(item["count"] for item in distribution) == len(rows), f"{key} universe mismatch")
        _assert(all(summary[key][grade] == count for grade, count in expected_counts.items()), f"{key} count mismatch")
        _assert(_build_dashboard_grade_distribution_chart(facts, key) is not None, f"{key} chart missing")
        spec = _build_dashboard_grade_distribution_chart(facts, key).to_dict()
        _assert(spec["layer"][0]["encoding"]["y"]["sort"] == expected_order, f"{key} chart sort mismatch")
        expected_scale = "sqrt" if key == "frequency_counts" else "linear"
        _assert(spec["layer"][0]["encoding"]["x"]["scale"]["type"] == expected_scale, f"{key} chart scale mismatch")
        _assert(spec["layer"][0]["encoding"]["x"]["field"] == "count", f"{key} count authority changed")
    restored = {"inventory": {"inventory_status_summary": {"total_product_count": 3}}}
    distribution = _dashboard_grade_distribution_rows(restored, "frequency_counts")
    _assert(sum(item["count"] for item in distribution) == 3, "frequency restored universe mismatch")
    _assert(distribution[-1]["count"] == 3, "frequency missing grade fallback mismatch")
    for key in ("profit_grade_counts", "contribution_grade_counts"):
        _assert(_dashboard_grade_distribution_rows(restored, key) == [], f"{key} must not invent missing grades")
    comparison = summary["frequency_month_comparison"]
    _assert(comparison["status"] == "ready", "two-month comparison is not ready")
    _assert(comparison["current_counts"]["F"] == 1, "current F count mismatch")
    _assert(comparison["previous_counts"]["X"] == 1, "previous X count mismatch")
    _assert(sum(comparison["current_counts"].values()) == 3, "current A-F/X total mismatch")
    _assert(sum(comparison["previous_counts"].values()) == 3, "previous A-F/X total mismatch")

    missing = _attach_inventory_status_and_frequency(
        [dict(row) for row in rows],
        frequency_snapshot=SnapshotReadResult(status="ready", generation_no=9),
        frequency_rows=current,
        frequency_evaluation_month="202609",
        previous_frequency_projection=FrequencyProjectionReadResult(status="missing", reason="no approved snapshot"),
        previous_frequency_evaluation_month="202608",
    )["summary"]["frequency_month_comparison"]
    _assert(missing["status"] == "previous_missing", "missing previous month must not be zero-filled")


def main() -> int:
    _staff_fixture()
    _frequency_fixture()
    print("PASS dashboard staff/frequency trend contract")
    print("staff exact/partial/AND; F/A-F/X counts; previous missing fail-closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

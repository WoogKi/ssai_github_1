"""Offline contract checks for the Dashboard inventory-status donuts."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_lite_facts import INVENTORY_STATUS_ORDER
from app.sims.views.dashboard_lite import (
    _INVENTORY_STATUS_COLORS,
    _dashboard_inline_icon,
    _build_dashboard_grade_distribution_chart,
    _dashboard_grade_distribution_rows,
    _inventory_status_change_html,
    _inventory_status_core_donut,
    _inventory_status_core_legend_html,
    _inventory_status_minor_html,
    build_dashboard_lite_chat_snapshot,
)


def main() -> None:
    styles_source = (ROOT / "app/sims/views/dashboard_lite.py").read_text(encoding="utf-8")
    assert "dashboard-lite-status-compare" not in styles_source
    assert "dashboard_inventory_minor_section__" in styles_source
    assert "dashboard_inventory_card__change__" not in styles_source
    assert "dashboard_inventory_donut_group__" in styles_source
    assert "dashboard-lite-icon-profit" in _dashboard_inline_icon("coins", "profit")
    assert "dashboard-lite-icon-contribution" in _dashboard_inline_icon("bars", "contribution")
    current = {label: 1 for label in INVENTORY_STATUS_ORDER}
    pending = dict(current)
    pending["긴급 부족"] = 0
    pending["과다 재고"] = 2
    facts = {"inventory": {"inventory_status_summary": {
        "total_product_count": 9,
        "status_counts": current,
        "pending_status_counts": pending,
        "pending_available": True,
    }}}

    for pending_flag in (False, True):
        chart = _inventory_status_core_donut(facts, pending=pending_flag)
        assert chart is not None
        spec = chart.to_dict()
        arc_rows = next(rows for rows in spec["datasets"].values() if rows and "sort_order" in rows[0])
        core = [row for row in arc_rows if row["label"] != "기타 재고 상태"]
        assert len(core) == (4 if pending_flag else 5)
        assert sum(row["count"] for row in core) == 5
        assert sum(row["count"] for row in arc_rows) == 9
        assert spec["layer"][0]["encoding"]["color"]["scale"]["domain"] == list(INVENTORY_STATUS_ORDER[4:]) + ["기타 재고 상태"]
        assert spec["layer"][0]["encoding"]["color"]["scale"]["range"] == [
            _INVENTORY_STATUS_COLORS[label][0] for label in INVENTORY_STATUS_ORDER[4:]
        ] + ["#e8edf2"]
        assert spec["layer"][0]["mark"]["outerRadius"] == 138
        assert spec["height"] == 350
    assert [_INVENTORY_STATUS_COLORS[label][0] for label in INVENTORY_STATUS_ORDER[4:]] == [
        "#dc2626", "#f97316", "#f5b700", "#16a34a", "#7c3aed"
    ]
    legend = _inventory_status_core_legend_html()
    assert legend.count('class="dashboard-lite-status-legend-item"') == 5
    assert [legend.index(label) for label in INVENTORY_STATUS_ORDER[4:]] == sorted(legend.index(label) for label in INVENTORY_STATUS_ORDER[4:])
    minor = _inventory_status_minor_html(facts)
    assert minor is not None and minor.count('class="dashboard-lite-status-minor-item"') == 4
    assert "현재 1개 → 예정 1개" in minor

    changes = _inventory_status_change_html(facts)
    assert changes is not None
    assert "부족재고" in changes and "2개 → 1개" in changes and "-1개 순증감" in changes
    assert "적정 재고" in changes and "1개 → 1개" in changes and "+0개 순증감" in changes
    assert "과다 재고" in changes and "1개 → 2개" in changes and "+1개 순증감" in changes

    facts["inventory"]["inventory_status_summary"]["pending_status_counts"]["과다 재고"] = 3
    assert _inventory_status_core_donut(facts, pending=True) is None
    assert _inventory_status_minor_html(facts) is None
    facts["inventory"]["inventory_status_summary"]["pending_available"] = False
    assert _inventory_status_core_donut(facts, pending=True) is None
    assert _inventory_status_minor_html(facts) is None
    assert _inventory_status_change_html(facts) is None

    empty_counts = {label: 0 for label in INVENTORY_STATUS_ORDER}
    facts["inventory"]["inventory_status_summary"].update({
        "total_product_count": 0,
        "status_counts": dict(empty_counts),
        "pending_status_counts": dict(empty_counts),
        "pending_available": True,
    })
    assert _inventory_status_core_donut(facts) is None
    dominant_counts = dict(empty_counts)
    dominant_counts["과다 재고"] = 9
    facts["inventory"]["inventory_status_summary"].update({
        "total_product_count": 9,
        "status_counts": dict(dominant_counts),
        "pending_status_counts": dict(dominant_counts),
    })
    assert _inventory_status_core_donut(facts) is not None
    assert _inventory_status_minor_html(facts).count('class="dashboard-lite-status-minor-item"') == 3
    assert "자료 부족 0건" in _inventory_status_minor_html(facts)

    # Counts from the supplied company-7 screen, not a fresh ERP query.
    current_counts = (0, 128, 63, 118, 2186, 370, 3940, 1986, 6563)
    pending_counts = (0, 133, 55, 126, 1937, 264, 3321, 2118, 7400)
    live_summary = facts["inventory"]["inventory_status_summary"]
    live_summary.update({
        "total_product_count": 15354,
        "status_counts": dict(zip(INVENTORY_STATUS_ORDER, current_counts)),
        "pending_status_counts": dict(zip(INVENTORY_STATUS_ORDER, pending_counts)),
        "pending_available": True,
    })
    assert sum(current_counts) == sum(pending_counts) == 15354
    current_donut = _inventory_status_core_donut(facts)
    pending_donut = _inventory_status_core_donut(facts, pending=True)
    live_changes = _inventory_status_change_html(facts)
    assert current_donut is not None and pending_donut is not None and live_changes is not None
    for chart, counts in ((current_donut, current_counts), (pending_donut, pending_counts)):
        spec = chart.to_dict()
        arc_rows = next(rows for rows in spec["datasets"].values() if rows and "sort_order" in rows[0])
        core = [row for row in arc_rows if row["label"] != "기타 재고 상태"]
        assert [row["count"] for row in core] == list(counts[4:])
        assert all(row["pct"] == row["count"] / 15354 * 100 for row in core)
        assert sum(row["count"] for row in core) + sum(counts[:4]) == 15354
        assert sum(row["count"] for row in arc_rows) == 15354
        assert arc_rows[-1]["count"] == sum(counts[:4])
        assert next(row for row in core if row["label"] == "과다 재고")["inside_count"] == f"{counts[-1]:,}개"
        assert next(row for row in core if row["label"] == "재고 부족")["inside_count"] == ""
        assert next(row for row in core if row["label"] == "재고 부족")["outside_label"].endswith("%")
        assert all("개" not in row["outside_label"] for row in core)
        assert any(rows and rows[0].get("total") == "15,354개" for rows in spec["datasets"].values())
    assert "6,563개" in str(current_donut.to_dict()) and "7,400개" in str(pending_donut.to_dict())
    assert "2,556개 → 2,201개" in live_changes and "-355개 순증감" in live_changes
    assert "1,986개 → 2,118개" in live_changes and "+132개 순증감" in live_changes
    assert "6,563개 → 7,400개" in live_changes and "+837개 순증감" in live_changes

    grade_counts = {"A": 2594, "B": 2594, "C": 2593, "D": 2594, "E": 2593, "X": 0, "등급자료 부족": 2386}
    live_summary["profit_grade_counts"] = dict(grade_counts)
    live_summary["contribution_grade_counts"] = dict(grade_counts)
    compact = build_dashboard_lite_chat_snapshot({"facts": facts, "params": {}})
    restored = compact["facts"]
    for key in ("profit_grade_counts", "contribution_grade_counts"):
        assert restored["inventory"]["inventory_status_summary"][key] == grade_counts
        rows = _dashboard_grade_distribution_rows(restored, key)
        assert {row["grade"]: row["count"] for row in rows} == grade_counts
        assert _build_dashboard_grade_distribution_chart(restored, key) is not None

    # Historical compact payloads without the fields must not become false 100% missing grades.
    legacy = {"inventory": {"inventory_status_summary": {"total_product_count": 15354}}}
    assert _dashboard_grade_distribution_rows(legacy, "profit_grade_counts") == []
    assert _dashboard_grade_distribution_rows(legacy, "contribution_grade_counts") == []
    assert _build_dashboard_grade_distribution_chart(legacy, "profit_grade_counts") is None
    print("dashboard inventory visual: PASS")


if __name__ == "__main__":
    main()

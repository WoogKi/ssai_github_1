"""Offline check that one inventory state is hidden without changing facts."""

from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_lite_facts import INVENTORY_STATUS_ORDER  # noqa: E402
from app.services.dashboard_risk_detail_export import (  # noqa: E402
    build_dashboard_inventory_detail_excel_bytes,
)
from app.sims.views.dashboard_lite import (  # noqa: E402
    _DISPLAY_INVENTORY_STATUS_ORDER,
    _filter_integrated_inventory_detail_rows,
    _inventory_status_core_donut,
    _inventory_status_minor_html,
    _inventory_status_rows,
    _monthly_depletion_cross_table,
)


def main() -> None:
    hidden = "예상수요 없음"
    assert hidden in INVENTORY_STATUS_ORDER
    assert hidden not in _DISPLAY_INVENTORY_STATUS_ORDER
    assert {"수요없는 재고", "자료 부족", "신제품 재고", "긴급 부족"}.issubset(
        _DISPLAY_INVENTORY_STATUS_ORDER
    )
    counts = {status: 0 for status in INVENTORY_STATUS_ORDER}
    counts.update({hidden: 1, "수요없는 재고": 1, "자료 부족": 1, "긴급 부족": 1})
    facts = {"inventory": {"inventory_status_summary": {
        "status_counts": dict(counts), "pending_status_counts": dict(counts),
        "pending_available": True, "total_product_count": 4,
    }, "monthly_depletion_summary": {"inventory_status_cross_rows": [
        {"재고상태": status, "월간 소진상태": "월말 부족 예상", "품목수": 1}
        for status in (hidden, "수요없는 재고", "자료 부족", "긴급 부족")
    ]}}}
    assert sum(row["count"] for row in _inventory_status_rows(facts)) == 4
    assert counts[hidden] == 1
    assert _inventory_status_core_donut(facts) is not None
    minor = _inventory_status_minor_html(facts)
    assert minor is not None and hidden not in minor and "수요없는 재고" in minor
    cross = _monthly_depletion_cross_table(facts)
    assert hidden not in cross.index and int(cross.loc["합계", "합계"]) == 3
    hidden_only = {"inventory": {"monthly_depletion_summary": {"inventory_status_cross_rows": [
        {"재고상태": hidden, "월간 소진상태": "월말 부족 예상", "품목수": 1}
    ]}}}
    assert _monthly_depletion_cross_table(hidden_only).empty

    source = pd.DataFrame([
        {"제품코드": str(index), "제품명": str(index), "재고상태": status,
         "위험 품목 여부": "비위험 품목", "재고보유영업일": index,
         "위험보정부족예상금액": 0}
        for index, status in enumerate((hidden, "수요없는 재고", "자료 부족", "긴급 부족"), 1)
    ])
    visible = _filter_integrated_inventory_detail_rows(
        source, inventory_status="전체", frequency_grade="전체", risk_filter="전체",
        vendor_key="전체", search_text="",
    )
    assert len(source) == 4 and len(visible) == 3
    assert hidden not in visible["재고상태"].tolist()
    assert source.iloc[0]["재고상태"] == hidden
    payload, info = build_dashboard_inventory_detail_excel_bytes(visible, [])
    assert info["export_rows"] == 3
    workbook = load_workbook(BytesIO(payload), read_only=True, data_only=True)
    try:
        rows = list(workbook.worksheets[0].values)
        status_index = rows[0].index("재고상태")
        assert len(rows) == 4
        assert hidden not in [row[status_index] for row in rows[1:]]
    finally:
        workbook.close()
    print("dashboard_no_expected_demand_display: PASS (facts 4, detail/Excel 3, no DB)")


if __name__ == "__main__":
    main()

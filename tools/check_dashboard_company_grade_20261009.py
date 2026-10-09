"""Offline company-grade scope versus selected-location quantity contract."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.dashboard_inventory_frequency_snapshot import FrequencyProjectionReadResult
from app.services.dashboard_lite_facts import _company_grade_snapshot_scope, _company_grade_stock_scope, build_dashboard_lite_facts
from tools.check_dashboard_business_day_progress import _ready_context, _sales_payload


def main() -> int:
    company_codes = ["00001", "00247", "00901"]
    company_scope = {
        "stock_cd_list": company_codes,
        "product_group_list": ["0013:0001"],
        "product_di_list": ["0004:1"],
        "product_class_list": ["0031:01"],
        "stock_mode": "book",
    }
    for selected in (["00001"], ["00247"], company_codes):
        codes, status = _company_grade_stock_scope({
            "company_id": "4", "stock_cd_list": selected,
            "_company_grade_scope_company_id": "4",
            "_company_grade_snapshot_scope": company_scope,
        })
        assert codes == company_codes and status == "company_saved"
    calls: list[int] = []

    def profile_loader(*, company_id: int) -> SimpleNamespace:
        calls.append(company_id)
        return SimpleNamespace(status="ready", profile={"stock_cd_list": ["0018:00004"]})

    codes, status = _company_grade_stock_scope({
        "company_id": "7", "_company_grade_scope_company_id": "4",
        "_company_grade_snapshot_scope": company_scope,
    }, profile_loader=profile_loader)
    assert codes == ["00004"] and status == "company_saved" and calls == [7]
    codes, status = _company_grade_stock_scope(
        {"company_id": "4"},
        profile_loader=lambda **_kwargs: SimpleNamespace(status="missing", profile=None),
    )
    assert codes == [] and status == "profile_unavailable"
    grade_scope, status = _company_grade_snapshot_scope({
        "company_id": "4", "_company_grade_scope_company_id": "4",
        "_company_grade_snapshot_scope": company_scope,
        "product_group_list": ["0013:other"], "stock_mode": "real",
    })
    assert status == "company_saved" and grade_scope["stock_mode"] == "book"
    assert grade_scope["product_group_list"] == ["0013:0001"]

    requested: list[dict] = []

    def projection_reader(**kwargs: object) -> FrequencyProjectionReadResult:
        requested.append(dict(kwargs))
        assert kwargs["stock_codes"] == company_codes
        assert kwargs["product_group_codes"] == ["0013:0001"]
        assert kwargs["product_di_codes"] == ["0004:1"]
        assert kwargs["product_class_codes"] == ["0031:01"]
        assert kwargs["stock_mode"] == "book"
        assert kwargs["product_codes"] == ["P1"]
        return FrequencyProjectionReadResult(
            status="ready", rows=({
                "product_code": "P1", "frequency_grade": "A",
                "profit_grade": "B", "contribution_grade": "C",
                "occurrence_count_3m": 5, "data_status": "ready",
            },), resolved_evaluation_month="202609",
        )

    context = _ready_context()
    for selected, stock, pending in (
        (["00001"], 0, 3), (["00247"], 7, 1), (company_codes, 11, 4),
    ):
        stock_frame = pd.DataFrame([{
            "제품코드": "P1", "제품명": "fixture", "현재재고수량": stock,
            "당월 잔여예상출고수량": 10, "부족예상수량": max(0, 10 - stock),
            "입고예정수량": pending,
            "입고예정 반영 부족수량": max(0, 10 - stock - pending),
        }])
        stock_frame.attrs["expected_inbound_attached"] = True
        facts = build_dashboard_lite_facts(
            {
                "company_id": "4", "month_from": "202603", "month_to": "202608",
                "evaluation_month": "202609", "policy_date": "20260911",
                "stock_cd_list": selected,
                "product_group_list": ["0013:other"],
                "product_di_list": ["0004:other"],
                "product_class_list": ["0031:other"],
                "stock_mode": "real",
                "_company_grade_scope_company_id": "4",
                "_company_grade_snapshot_scope": company_scope,
            },
            manufacturer_summary_payload=_sales_payload(),
            stock_shortage_payload={"df": stock_frame, "meta": {"expected_inbound_attached": True}},
            inbound_facts_df=pd.DataFrame(),
            frequency_projection_reader=projection_reader,
            today=date(2026, 9, 11),
            business_day_context_loader=lambda **_kwargs: context,
        )
        detail = facts["inventory"]["inventory_status_detail_rows"]
        assert len(detail) == 1, detail
        assert detail[0]["출고빈도등급"] == "A"
        assert detail[0]["품목손익등급"] == "B"
        assert detail[0]["품목기여등급"] == "C"
        assert detail[0]["현재재고수량"] == stock
        assert detail[0]["입고예정수량"] == pending
        assert facts["source_call_count"] == 0
    assert len(requested) == 3
    missing_calls = []

    def missing_reader(**kwargs: object) -> FrequencyProjectionReadResult:
        missing_calls.append(dict(kwargs))
        return FrequencyProjectionReadResult(status="missing", reason="exact_key_missing")

    missing = build_dashboard_lite_facts(
        {
            "company_id": "4", "month_from": "202603", "month_to": "202608",
            "evaluation_month": "202609", "policy_date": "20260911",
            "stock_cd_list": ["00001"],
            "_company_grade_scope_company_id": "4",
            "_company_grade_snapshot_scope": company_scope,
        },
        manufacturer_summary_payload=_sales_payload(),
        stock_shortage_payload={"df": stock_frame, "meta": {"expected_inbound_attached": True}},
        inbound_facts_df=pd.DataFrame(),
        frequency_projection_reader=missing_reader,
        today=date(2026, 9, 11),
        business_day_context_loader=lambda **_kwargs: context,
    )
    missing_detail = missing["inventory"]["inventory_status_detail_rows"]
    assert len(missing_calls) == 1 and missing_calls[0]["evaluation_month"] == "202609"
    assert missing_calls[0]["stock_codes"] == company_codes
    assert missing_detail[0]["출고빈도등급"] == "빈도자료 부족"
    assert missing_detail[0]["품목손익등급"] == "등급자료 부족"
    assert missing_detail[0]["품목기여등급"] == "등급자료 부족"
    print("PASS company-grade Snapshot scope, selected-location quantities, company isolation; ERP calls 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""DB-free regression for the isolated order-calculation Snapshot boundary."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services import order_calculation_service as module


def test_snapshot_request():
    from app.services import snapshot_product_information_service as product_info
    from app.services import dashboard_inventory_frequency_snapshot_service as frequency
    from app.services import business_calendar_service as calendar

    params = {
        "company_id": 4,
        "order_date": "2026-10-23",
        "date_from": "20261001",
        "date_to": "20261023",
        "month_from": "202610",
        "month_to": "202610",
        "order_staff_nm": "테스트담당자",
        "closing_day": 20,
        "stock_cd_list": ["00001", "00901"],
        "_order_stock_scope_resolved": True,
        "_order_stock_scope_policy": "saved",
    }
    scope = SimpleNamespace(
        stock_codes=["00001", "00901"],
        io_gu_codes=["001", "500"],
        product_group_codes=[], product_di_codes=[], product_class_codes=[],
        stock_mode="real",
    )
    seen = {}

    def snapshot(query, **kwargs):
        seen.update(query)
        assert kwargs["apply_viewer_projection"] is False
        assert kwargs["materialize_presentation"] is False
        return {"meta": {"snapshot_status": "ready"}, "df": pd.DataFrame()}

    with patch.object(product_info, "get_snapshot_product_information_result", side_effect=snapshot), \
         patch.object(frequency, "resolve_dashboard_profile_stock_scope", return_value=scope), \
         patch.object(calendar, "next_business_day_on_or_after", return_value=SimpleNamespace(
             status="ready", effective_date="20261023")), \
         patch.object(module, "resolve_saved_io_scope", return_value=object()):
        result = module.load_sources(dict(params))
    assert result["snapshot"]["meta"]["snapshot_status"] == "ready"
    assert set(params).difference(seen) == {"date_from", "date_to", "month_from", "month_to"}
    assert seen["evaluation_month"] == "202610"
    assert seen["order_staff_nm"] == "테스트담당자"
    assert seen["closing_day"] == 20
    assert seen["stock_cd_list"] == ["00001", "00901"]
    assert all(params[key] == value for key, value in (
        ("date_from", "20261001"), ("date_to", "20261023"),
        ("month_from", "202610"), ("month_to", "202610"),
    ))

    # Direct product-information queries must still reject an explicit period.
    calls = []
    with patch.object(product_info, "get_current_company_id", return_value=4):
        direct = product_info._get_snapshot_product_information_result(
            {"company_id": 4, "date_from": "20261001"},
            profile_resolver=lambda **kwargs: calls.append(kwargs),
        )
    assert direct["meta"]["result_status"] == "input_required"
    assert direct["meta"]["snapshot_read_call_count"] == 0
    assert direct["meta"]["source_call_count"] == 0
    assert not calls


def test_after_snapshot():
    from tools.check_order_calculation_contract import fixture

    params, sources = fixture()
    sources["suppliers"]["recent_inbound_vendor_staff_name"] = "테스트담당자"
    with patch.object(module, "get_current_company_id", return_value=params["company_id"]), \
         patch.object(module, "write_manual_validation_evidence", return_value=None):
        result = module.get_order_calculation_result(
            {**params, "order_staff_nm": "테스트담당자", "closing_day": 20, "query_mode": "전체"},
            source_loader=lambda query: sources,
        )
    assert result["meta"]["result_status"] == "success"
    assert len(result["df"]) == 1, (result["meta"], result["df"].to_dict("records"))
    assert result["df"].iloc[0]["결제일자"] == 20


if __name__ == "__main__":
    test_snapshot_request()
    test_after_snapshot()
    print("ORDER_SNAPSHOT_BOUNDARY_PASS")

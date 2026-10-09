"""Offline Dashboard NLQ stock-location scope regression."""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    from app.sims.nlq import nlq_router as router
    from app.services import dashboard_lite_facts, io_nlq, ssai_analysis_profile_service
    from app.ui import ssai_login

    profiles = {
        4: {"stock_cd_list": ["00001", "00247", "00901"], "io_gu_list": ["001"]},
        8: {"stock_cd_list": ["00111"], "io_gu_list": ["002"]},
    }
    names = {"00001": ".본사 창고", "00247": "(전주)창고", "00901": ".오토스토어 대기창고"}
    lookups: list[str] = []

    def build(question: str, company: int = 4):
        with (
            patch.object(ssai_login, "get_selected_company", return_value={"company_id": company}),
            patch.object(ssai_analysis_profile_service, "load_dashboard_profile", side_effect=lambda *, company_id: profiles[company_id]),
            patch.object(dashboard_lite_facts, "normalize_dashboard_lite_params", side_effect=lambda value: value),
            patch.object(io_nlq, "get_current_stock_location_name_map", side_effect=lambda: lookups.append(question) or names),
        ):
            return router._build_dashboard_nlq_params(question, session_state={}, logger=logging.getLogger(__name__))

    for question, expected in (
        ("SIMS 일일점검", ["00001", "00247", "00901"]),
        ("SIMS 일일점검 재고위치 00247", ["00247"]),
        ("SIMS 일일점검 재고위치 전주", ["00247"]),
        ("SIMS 일일점검 재고위치 본사, 오토", ["00001", "00901"]),
        ("SIMS 일일점검 재고위치 00001,00901", ["00001", "00901"]),
        ("SIMS 일일점검 재고위치 00001 + 오토", ["00001", "00901"]),
        ("SIMS 일일점검 재고위치 본사/00901", ["00901", "00001"]),
        ("SIMS 일일점검 재고위치 본사와 오토", ["00001", "00901"]),
        ("SIMS 일일점검 재고위치 00001,00001", ["00001"]),
    ):
        params, notice = build(question)
        assert notice is None, (question, notice)
        assert params["stock_cd_list"] == expected, (question, params["stock_cd_list"])
        assert params["io_gu_list"] == ["001"], question

    for question in (
        "SIMS 일일점검 재고위치 00004",
        "SIMS 일일점검 재고위치 전주, 미등록",
        "SIMS 일일점검 재고위치 전체창고",
        "SIMS 일일점검 전체창고",
    ):
        params, notice = build(question)
        assert not params and notice["meta"]["result_status"] == "input_required", question
        assert notice["meta"]["source_call_count"] == 0, question
        assert "00001" in notice["message"] or "기본 위치" in notice["message"], question

    params, notice = build("SIMS 일일점검 재고위치 본사", company=8)
    assert not params and notice["meta"]["result_status"] == "input_required"
    assert "00111" in notice["message"] and "00001" not in notice["message"]
    assert len(lookups) >= 3

    from datetime import date
    from app.services import analytics_sales_trend_service, dashboard_inbound_facts_service

    params, notice = build("SIMS 일일점검 재고위치 본사, 오토")
    assert notice is None
    normalized = dashboard_lite_facts.normalize_dashboard_lite_params(params, today=date(2026, 10, 9))
    source_params = dashboard_lite_facts._dashboard_internal_source_params(normalized, today=date(2026, 10, 9))
    assert normalized["stock_cd_list"] == source_params["stock_cd_list"] == ["00001", "00901"]
    inbound_sql, inbound_binds = dashboard_inbound_facts_service._sql(
        normalized, start_date="20260101", cutoff_date="20261009"
    )
    assert "Rd11_Stock_Cd" in inbound_sql
    assert {value for key, value in inbound_binds.items() if key.startswith("stock_cd_")} == {"00001", "00901"}
    spec = analytics_sales_trend_service._monthly_spec(source_params["source_mode"])
    for make_where in (
        analytics_sales_trend_service._build_monthly_fast_where,
        analytics_sales_trend_service._build_dashboard_purchase_vendor_where,
    ):
        where_sql, binds = make_where(dict(source_params), spec)
        assert "_Stock_Cd IN" in where_sql
        assert {value for key, value in binds.items() if "stock_cd_" in key and isinstance(value, str)} == {"00001", "00901"}
    from app.sims.views import dashboard_lite
    from app.ui import chat_middleware

    cache = {
        "params": params,
        "facts": {"source_call_count": 3, "inventory": {"readiness_rows": [{}]}},
    }
    with (
        patch.object(router, "_build_dashboard_nlq_params", return_value=(params, None)),
        patch.object(dashboard_lite, "build_dashboard_lite_result_payload", return_value=({"meta": {}}, cache)) as facts_builder,
        patch.object(dashboard_lite, "dashboard_request_publish_allowed", return_value=True),
        patch.object(chat_middleware, "get_current_chat_room_id", return_value="room-4"),
        patch.object(chat_middleware, "push_sims_result_to_chat") as push,
    ):
        assert router._try_handle_dashboard_nlq(
            "SIMS 일일점검 재고위치 본사, 오토", room={"id": "room-4"},
            session_state={}, logger=logging.getLogger(__name__),
        )
    assert facts_builder.call_count == push.call_count == 1
    assert facts_builder.call_args.args[0]["stock_cd_list"] == ["00001", "00901"]
    assert cache["facts"]["source_call_count"] == 3
    print("PASS Dashboard NLQ location scope; source calls on blocked requests=0; DB connections=0")


if __name__ == "__main__":
    main()

"""Offline Dashboard stock-location order and fail-closed regression."""

import logging
from unittest.mock import patch

from app.services import dashboard_lite_facts, io_nlq, ssai_analysis_profile_service
from app.sims.nlq import nlq_router
from app.ui import ssai_login


def main() -> None:
    profile = {"stock_cd_list": ["00001", "00008"], "io_gu_list": ["001"]}
    names = {"00001": "본사", "00008": "안양"}

    def build(question: str):
        with (
            patch.object(ssai_login, "get_selected_company", return_value={"company_id": 3}),
            patch.object(ssai_analysis_profile_service, "load_dashboard_profile", return_value=profile),
            patch.object(dashboard_lite_facts, "normalize_dashboard_lite_params", side_effect=lambda value: value),
            patch.object(io_nlq, "get_current_stock_location_name_map", return_value=names),
        ):
            return nlq_router._build_dashboard_nlq_params(
                question, session_state={}, logger=logging.getLogger(__name__),
            )

    pairs = (
        ("재고위치 안양 sims 일일점검", "sims 일일점검 재고위치 안양"),
        ("재고위치 00008 sims 일일점검", "sims 일일점검 재고위치 00008"),
    )
    for before, after in pairs:
        for question in (before, after):
            assert nlq_router._resolve_dashboard_nlq_action(question) == "SIMS 일일점검"
            conditions, residual = nlq_router._extract_dashboard_nlq_conditions(question)
            assert conditions["재고위치"] in {"안양", "00008"} and not residual
            params, notice = build(question)
            assert notice is None, (question, notice)
            assert params["stock_cd_list"] == ["00008"], (question, params)
            assert params["company_id"] == "3"
        assert build(before)[0]["stock_cd_list"] == build(after)[0]["stock_cd_list"]

    invalid = "재고위치 없는위치 sims 일일점검"
    params, notice = build(invalid)
    assert not params and notice["meta"]["result_status"] == "input_required"
    assert notice["meta"]["source_call_count"] == 0
    assert "없는위치" not in str(notice.get("params") or {})
    from app.sims.views import dashboard_lite
    from app.ui import chat_middleware

    with (
        patch.object(nlq_router, "_build_dashboard_nlq_params", return_value=(params, notice)),
        patch.object(dashboard_lite, "build_dashboard_lite_result_payload") as build_facts,
        patch.object(chat_middleware, "push_sims_result_to_chat") as push,
    ):
        assert nlq_router._try_handle_dashboard_nlq(
            invalid, room={}, session_state={}, logger=logging.getLogger(__name__),
        )
        build_facts.assert_not_called()
        assert push.call_count == 1
        assert push.call_args.args[0]["meta"]["source_call_count"] == 0

    default_params, default_notice = build("SIMS 일일점검")
    assert default_notice is None and default_params["stock_cd_list"] == ["00001", "00008"]
    print("PASS: Dashboard location before/after action, invalid location blocked, DB calls=0")


if __name__ == "__main__":
    main()

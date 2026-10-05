"""Offline regression for Dashboard NLQ phrase whitespace normalization."""

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
    from app.services import dashboard_lite_facts, product_supplier_scope_service
    from app.services import ssai_analysis_profile_service
    from app.ui import ssai_login

    action = "SIMS 일일점검"
    bare_variants = (
        "SIMS 일일점검",
        "SIMS  일일점검",
        "SIMS   일일점검",
        "SIMS일일점검",
        "SIMS\t일일점검",
        "SIMS\u00a0일일점검",
        "오늘의 경영점검",
        "오늘의  경영점검",
        "오늘의경영점검",
        "SIMS 운영점검",
        "SIMS  운영점검",
    )
    supplier_variants = (
        ("SIMS 일일점검 한미", "한미", "manufacturer"),
        ("SIMS  일일점검  한미", "한미", "manufacturer"),
        ("SIMS 일일점검 제약사 한미", "한미", "manufacturer"),
        ("SIMS 운영점검 발주처 종근당", "종근당", "order_vendor"),
    )

    assert all(router._resolve_dashboard_nlq_action(query) == action for query in bare_variants)
    assert all(router._dashboard_nlq_residual(query) == "" for query in bare_variants)
    assert router._extract_dashboard_nlq_conditions("오늘의 경영점검 담당자 김")[0]["담당자"] == "김"

    candidate = router.resolve_new_sims_nlq_candidate("sims 일일점검") or {}
    assert candidate == {"route": "dashboard", "action": action}, candidate
    from app.sims.views import dashboard_lite
    from app.ui import chat_middleware

    for query in bare_variants:
        for current_table in (None, {"company_id": "3", "rows": []}):
            state = {"__sims_current_table": current_table} if current_table else {}
            cache = {"params": {"company_id": "3"}, "facts": {"source_call_count": 0, "inventory": {"readiness_rows": [{}]}}}
            with (
                patch.object(router, "_build_dashboard_nlq_params", return_value=({"company_id": "3"}, None)) as params_builder,
                patch.object(dashboard_lite, "build_dashboard_lite_result_payload", return_value=({"meta": {}}, cache)) as facts_builder,
                patch.object(dashboard_lite, "dashboard_request_publish_allowed", return_value=True),
                patch.object(chat_middleware, "get_current_chat_room_id", return_value="room-3"),
                patch.object(chat_middleware, "push_sims_result_to_chat") as push,
            ):
                handled = router.try_handle_nlq(
                    query, room={"id": "room-3"}, session_state=state,
                    make_ts=lambda: "fixture", next_seq=lambda: 1,
                    logger=logging.getLogger(__name__),
                )
            assert handled and params_builder.call_count == 1, query
            assert params_builder.call_args.args[0] == query, query
            assert facts_builder.call_count == 1 and push.call_count == 1, query
    assert router.resolve_new_sims_nlq_candidate("일일점검") is None

    from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
    from app.services.io_nlq import resolve_io_nlq

    for query, action_name in (
        ("sims 발주담당자 윤정아 발주조회 202609", "발주조회"),
        ("sims 발주담당자 윤정아 발주계산", "발주 계산"),
    ):
        routed: list[str] = []

        def capture_io(text, **_kwargs):
            routed.append(text)
            return True

        with (
            patch.object(router, "_try_handle_io_nlq", side_effect=capture_io),
            patch.object(router, "_try_handle_dashboard_nlq", return_value=False),
        ):
            assert router.try_handle_nlq(
                query, room={}, session_state={}, make_ts=lambda: "fixture",
                next_seq=lambda: 1, logger=logging.getLogger(__name__),
            ), query
        assert routed == [query[5:]], (query, routed)
        parsed = resolve_registered_erp_table_nlq(routed[0]) or {}
        io_parsed = resolve_io_nlq(routed[0]) or {}
        for result in (parsed, io_parsed):
            assert result.get("action") == action_name, (query, result)
            assert (result.get("params") or {}).get("order_staff_nm") == "윤정아", (query, result)

    supplier_calls: list[tuple[str, str]] = []

    def resolve_supplier(text: str, *, mode: str):
        supplier_calls.append((text, mode))
        return [{"code": "10047", "name": text}]

    with (
        patch.object(ssai_login, "get_selected_company", return_value={"company_id": 3}),
        patch.object(ssai_analysis_profile_service, "load_dashboard_profile", return_value={}),
        patch.object(dashboard_lite_facts, "default_dashboard_lite_scope", return_value={}),
        patch.object(dashboard_lite_facts, "normalize_dashboard_lite_params", side_effect=lambda value: value),
        patch.object(product_supplier_scope_service, "resolve_supplier_vendor_codes", side_effect=resolve_supplier),
    ):
        for query in bare_variants:
            params, notice = router._build_dashboard_nlq_params(query, session_state={}, logger=logging.getLogger(__name__))
            assert notice is None, query
            assert not (params.get("manufacturer_codes") or params.get("order_vendor_codes")), query
        assert supplier_calls == [], supplier_calls

        for query, expected_supplier, expected_mode in supplier_variants:
            params, notice = router._build_dashboard_nlq_params(query, session_state={}, logger=logging.getLogger(__name__))
            assert notice is None, query
            assert params.get("product_supplier_scope_mode") == expected_mode, query
            expected_key = "manufacturer_codes" if expected_mode == "manufacturer" else "order_vendor_codes"
            assert params.get(expected_key) == ["10047"], query
            assert supplier_calls[-1] == (expected_supplier, expected_mode), supplier_calls[-1]

    print("PASS dashboard NLQ whitespace normalization; DB connection attempts=0")


if __name__ == "__main__":
    main()

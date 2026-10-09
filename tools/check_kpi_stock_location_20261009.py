"""DB-free KPI stock-location authority and production-router regression."""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import analytics_sales_trend_service as trend
from app.sims.nlq import nlq_router as router


def main() -> int:
    action = "품목별 매출 추세 분석"
    profile = {
        "stock_mode": "real", "stock_cd_list": ["00001", "00247", "00901"],
        "io_gu_list": ["0012:051"],
    }
    names = {"00001": ".본사 창고", "00247": "(전주)창고", "00901": ".오토스토어 대기창고", "00004": "본사 임시창고"}
    cases = (
        ("품목별매출추세분석", "success", ["00001", "00247", "00901"]),
        ("품목별매출추세분석 재고위치 00001", "success", ["00001"]),
        ("품목별매출추세분석 재고위치 본사창고", "success", ["00001"]),
        ("품목별매출추세분석 재고위치 본사", "success", ["00001"]),
        ("품목별매출추세분석 재고위치 전주", "success", ["00247"]),
        ("품목별매출추세분석 재고위치 오토스토어", "success", ["00901"]),
        ("품목별매출추세분석 재고위치 본사, 오토", "success", ["00001", "00901"]),
        ("품목별매출추세분석 재고위치 본사 + 오토", "success", ["00001", "00901"]),
        ("품목별매출추세분석 재고위치 본사와 오토", "success", ["00001", "00901"]),
        ("품목별매출추세분석 재고위치 본사 과 오토", "success", ["00001", "00901"]),
        ("품목별매출추세분석 재고위치 00001,00901", "success", ["00001", "00901"]),
        ("품목별매출추세분석 재고위치 00001,00247", "success", ["00001", "00247"]),
        ("품목별매출추세분석 재고위치 00001 + 00901", "success", ["00001", "00901"]),
        ("품목별매출추세분석 재고위치 00001/00901", "success", ["00001", "00901"]),
        ("품목별매출추세분석 재고위치 00001, 오토", "success", ["00001", "00901"]),
        ("품목별매출추세분석 재고위치 본사, 00901", "success", ["00901", "00001"]),
        ("품목별매출추세분석 재고위치 00001,00001", "success", ["00001"]),
        ("품목별매출추세분석 재고위치 본사, 00004", "input_required", None),
        ("품목별매출추세분석 재고위치 본사, 미등록", "input_required", None),
        ("품목별매출추세분석 재고위치 본사 +", "input_required", None),
        ("품목별매출추세분석 재고위치 임시창고", "input_required", None),
        ("품목별매출추세분석 재고위치 00004", "input_required", None),
        ("품목별매출추세분석 재고위치 99999", "input_required", None),
        ("품목별매출추세분석 재고위치 알수없음", "input_required", None),
        ("품목별매출추세분석 장부재고 재고위치 00001", "success", ["00001"]),
    )
    results = []

    def service(params):
        calls.append(dict(params))
        return {"final": True, "type": "text", "title": action, "action": action,
                "params": dict(params), "data": "fixture", "message": "fixture",
                "meta": {"result_status": "success", "row_count": 1}}

    for question, status, expected_codes in cases:
        calls = []
        delivered = []
        with (
            patch("app.ui.ssai_login.get_selected_company", return_value={"company_id": 4}),
            patch("app.services.ssai_analysis_profile_service.load_dashboard_profile", return_value=profile),
            patch.object(router, "_analytics_nlq_option_codes", side_effect=lambda field: names if field == "stock_cd_list" else []),
            patch("app.services.io_nlq.get_current_stock_location_name_map", return_value=names),
            patch.object(router, "_get_analytics_handler", return_value=service),
            patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, _action: delivered.append(payload)),
            patch("app.db.mssql_client.get_conn", side_effect=AssertionError("ERP connection attempted")) as connection,
        ):
            handled = router._try_handle_analytics_nlq(
                question, room={"messages": []}, session_state={},
                make_ts=lambda: "fixture", next_seq=lambda: 1,
                logger=logging.getLogger(__name__),
            )
        payload = delivered[-1] if delivered else {}
        actual_status = str((payload.get("meta") or {}).get("result_status") or "")
        ok = handled and actual_status == status and connection.call_count == 0
        if expected_codes is None:
            message = str(payload.get("message") or "")
            ok = ok and not calls and ("선택 가능한 위치" in message or "다시 지정" in message)
            ok = ok and bool((payload.get("meta") or {}).get("service_call_skipped"))
        else:
            params = calls[0] if len(calls) == 1 else {}
            ok = ok and params.get("stock_cd_list") == expected_codes and not params.get("stock_nm")
            ok = ok and params.get("io_gu_list") == ["051"]
            ok = ok and params.get("stock_mode") == ("book" if "장부재고" in question else "real")
            if len(expected_codes) == 1:
                sql_params = dict(params)
                filters = trend._build_monthly_filters(sql_params, trend._monthly_spec("monthly_book" if "장부재고" in question else "monthly_real"))
                ok = ok and "_Stock_Cd" in filters and "Stock_Cd.Rd01_Hnm LIKE" not in filters
                detail_filters = trend._build_filters(dict(params))
                ok = ok and "Rd12_Stock_Cd" in detail_filters and "Stock_Cd.Rd01_Hnm LIKE" not in detail_filters
        results.append((question, ok, actual_status, len(calls)))

    other_company, other_error = router._resolve_analytics_stock_location_scope(
        {"stock_cd": "00001", "stock_cds": ["00001"]},
        saved_codes=["00999"], location_names={"00999": "다른 회사 위치"},
    )
    results.append(("회사 분리: 미저장 위치", other_error == "outside_saved_locations",
                    other_error, 0))

    exact, exact_error = router._resolve_analytics_stock_location_scope(
        {"stock_nm": "본사"}, saved_codes=["00001", "00002"],
        location_names={"00001": ".본사 창고", "00002": ".본사"},
    )
    results.append(("이름 정확 일치 우선", not exact_error and exact.get("stock_cd_list") == ["00002"], exact_error, 0))
    ambiguous, ambiguous_error = router._resolve_analytics_stock_location_scope(
        {"stock_nm": "본사"}, saved_codes=["00001", "00002"],
        location_names={"00001": ".본사 창고", "00002": ".본사 대기창고"},
    )
    results.append(("저장 위치 부분일치 복수 후보", ambiguous_error == "location_name_ambiguous"
                    and ambiguous.get("__analytics_stock_candidates") == ["00001", "00002"], ambiguous_error, 0))

    ambiguous_calls = []
    ambiguous_delivery = []
    ambiguous_profile = {**profile, "stock_cd_list": ["00001", "00002"]}
    ambiguous_names = {"00001": ".본사 창고", "00002": ".본사 대기창고"}
    with (
        patch("app.ui.ssai_login.get_selected_company", return_value={"company_id": 4}),
        patch("app.services.ssai_analysis_profile_service.load_dashboard_profile", return_value=ambiguous_profile),
        patch.object(router, "_analytics_nlq_option_codes", side_effect=lambda field: ambiguous_names if field == "stock_cd_list" else []),
        patch("app.services.io_nlq.get_current_stock_location_name_map", return_value=ambiguous_names),
        patch.object(router, "_get_analytics_handler", return_value=lambda params: ambiguous_calls.append(params)),
        patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, _action: ambiguous_delivery.append(payload)),
        patch("app.db.mssql_client.get_conn", side_effect=AssertionError("ERP connection attempted")) as connection,
    ):
        handled = router._try_handle_analytics_nlq(
            "품목별매출추세분석 재고위치 본사", room={"messages": []},
            session_state={}, make_ts=lambda: "fixture", next_seq=lambda: 1,
            logger=logging.getLogger(__name__),
        )
    notice = ambiguous_delivery[-1] if ambiguous_delivery else {}
    message = str(notice.get("message") or "")
    blocked = handled and not ambiguous_calls and connection.call_count == 0
    blocked = blocked and (notice.get("meta") or {}).get("result_status") == "input_required"
    blocked = blocked and all(code in message for code in ("00001", "00002"))
    blocked = blocked and "정확한 이름" in message
    results.append(("복수 후보 라우터 안내", blocked,
                    str((notice.get("meta") or {}).get("result_status") or ""), len(ambiguous_calls)))

    for term in ("전체창고", "모든창고", "전창고", "전체재고위치"):
        blocked_calls = []
        blocked_delivery = []
        with (
            patch.object(router, "_get_analytics_handler", return_value=lambda params: blocked_calls.append(params)),
            patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, _action: blocked_delivery.append(payload)),
            patch("app.db.mssql_client.get_conn", side_effect=AssertionError("ERP connection attempted")) as connection,
        ):
            handled = router._try_handle_analytics_nlq(
                f"품목별매출추세분석 {term}", room={"messages": []},
                session_state={}, make_ts=lambda: "fixture", next_seq=lambda: 1,
                logger=logging.getLogger(__name__),
            )
        notice = blocked_delivery[-1] if blocked_delivery else {}
        ok = handled and not blocked_calls and connection.call_count == 0
        ok = ok and (notice.get("meta") or {}).get("stock_scope_reason") == "all_stock_unsupported"
        ok = ok and "저장된 기본 위치" in str(notice.get("message") or "")
        results.append((f"KPI 전체창고 명령 차단: {term}", ok,
                        str((notice.get("meta") or {}).get("result_status") or ""), len(blocked_calls)))

    grouping_params = router._build_analytics_params(
        "품목별매출추세분석 재고위치별 집계", action,
    )
    results.append(("재고위치별 grouping 비간섭",
                    not grouping_params.get("stock_nm_list") and not grouping_params.get("stock_cd_list"),
                    "grouping", 0))
    compound_params = router._build_analytics_params(
        "품목별매출추세분석 재고위치 본사, 오토 제품구분 일반", action,
    )
    results.append(("위치 목록과 다른 조건 경계",
                    compound_params.get("stock_nm_list") == ["본사", "오토"],
                    "compound", 0))

    stock_actions = [
        action_name for action_name, supported in router._ANALYTICS_NLQ_DEFAULT_KEYS.items()
        if "stock_cd_list" in supported
    ]
    for action_name in stock_actions:
        question = f"{action_name} 재고위치 본사, 오토"
        parsed = router._build_analytics_params(question, action_name)
        with (
            patch("app.ui.ssai_login.get_selected_company", return_value={"company_id": 4}),
            patch("app.services.ssai_analysis_profile_service.load_dashboard_profile", return_value=profile),
            patch.object(router, "_analytics_nlq_option_codes", side_effect=lambda field: names if field == "stock_cd_list" else []),
            patch("app.services.io_nlq.get_current_stock_location_name_map", return_value=names),
            patch("app.db.mssql_client.get_conn", side_effect=AssertionError("ERP connection attempted")) as connection,
        ):
            applied = router._apply_company_default_to_analytics_nlq(
                parsed, text=question, action=action_name,
                session_state={}, logger=logging.getLogger(__name__),
            )
        ok = applied.get("stock_cd_list") == ["00001", "00901"]
        ok = ok and applied.get("io_gu_list") == ["051"] and applied.get("stock_mode") == "real"
        ok = ok and not applied.get("stock_nm") and connection.call_count == 0
        results.append((f"공통 KPI action: {action_name}", ok, "scope", 0))

    for unsupported_action in ("매출처별 매출 예상", "영업사원별 매출 예상", "지역별 매출 예상"):
        unsupported_calls = []
        unsupported_delivery = []
        with (
            patch.object(router, "_get_analytics_handler", return_value=lambda params: unsupported_calls.append(params)),
            patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, _action: unsupported_delivery.append(payload)),
            patch("app.db.mssql_client.get_conn", side_effect=AssertionError("ERP connection attempted")) as connection,
        ):
            handled = router._try_handle_analytics_nlq(
                f"{unsupported_action} 재고위치 00001", room={"messages": []},
                session_state={}, make_ts=lambda: "fixture", next_seq=lambda: 1,
                logger=logging.getLogger(__name__),
            )
        notice = unsupported_delivery[-1] if unsupported_delivery else {}
        blocked = handled and not unsupported_calls and connection.call_count == 0
        blocked = blocked and (notice.get("meta") or {}).get("result_status") == "input_required"
        blocked = blocked and "지원하지 않습니다" in str(notice.get("message") or "")
        results.append((f"미지원 분석의 재고위치 조건: {unsupported_action}", blocked,
                        str((notice.get("meta") or {}).get("result_status") or ""), len(unsupported_calls)))

    for question, ok, actual_status, count in results:
        print(f"{'PASS' if ok else 'FAIL'} {question}: status={actual_status} service_calls={count}")
    print(f"SUMMARY {sum(ok for _, ok, _, _ in results)}/{len(results)} PASS")
    return 0 if all(ok for _, ok, _, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

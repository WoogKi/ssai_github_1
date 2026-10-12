"""No-DB regression for explicit actions and unconsumed NLQ conditions."""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.erp_table_nlq import is_order_calculation_request
from app.services.io_nlq import resolve_io_nlq
from app.services.order_stock_location_scope import resolve_order_stock_scope
from app.sims.nlq import nlq_router, nlq_users
from app.ui.knowledge_chat_adapter import (
    parse_business_help_knowledge_request,
    parse_incomplete_sims_help_request,
    verified_sims_help_text,
)


def main() -> int:
    unsafe = {
        "SIMS 일일점점 재고위치 전주": "action_confirmation",
        "SIMS 일일점잔 재고위치 전주": "action_confirmation",
        "발주처기준 가상제약(*전문) 현재고금액 알려줘": "condition_confirmation",
        "10/08 발주 조회 가상담당자": "condition_confirmation",
        "10월 08일 발주 조회 가상담당자": "condition_confirmation",
        "발주담당자 가상담당자 발주계산 미등록창고": "condition_confirmation",
        "발주계산 발주담당자 가상담당자 미등록창고": "condition_confirmation",
        "발주 계산 발주담당자 가상담당자 미등록창고": "condition_confirmation",
        "발주담당자 가상담당자 미등록창고 발주계산": "condition_confirmation",
        "발주처 가상제약(전문) 10월 매출금액 현재고금액 알려줘": "single_action_required",
        "영업사원별 매출현황": "action_confirmation",
    }
    for query, reason in unsafe.items():
        intent = parse_incomplete_sims_help_request(query)
        assert intent is not None and intent.reason == reason, query
        answer = verified_sims_help_text(intent, allowed_actions=set())
        assert "SIMS 질문 예시" in answer and "조회는 조건을 선택" in answer, query
        with patch.object(nlq_router, "_try_handle_io_nlq", side_effect=AssertionError("ERP route")):
            assert not nlq_router.try_handle_nlq(
                query, room={"messages": []}, session_state={}, make_ts=lambda: "now",
                next_seq=lambda: 1, logger=logging.getLogger(__name__),
            ), query

    safe = {
        "SIMS 일일점검 재고위치 전주": "SIMS 일일점검",
        "사용자목록 재고위치 본사": "사용자목록 + 부서명",
        "사용자목록 재고위치 본사창고": "사용자목록 + 부서명",
        "발주담당자 가상담당자 발주계산 재고위치 본사창고": "발주 계산",
        "발주계산 발주담당자 가상담당자 재고위치 본사창고": "발주 계산",
        "발주계산 발주담당자 가상담당자": "발주 계산",
        "발주담당자 가상담당자 발주계산": "발주 계산",
        "발주 조회": "발주조회",
        "현재고 조회": "현재고 조회",
    }
    for query, action in safe.items():
        assert parse_incomplete_sims_help_request(query) is None, query
        assert (nlq_router.resolve_new_sims_nlq_candidate(query) or {}).get("action") == action, query

    for query in (
        "발주계산 발주담당자 가상담당자 미등록창고",
        "발주담당자 가상담당자 발주계산 미등록창고",
        "발주 계산 발주담당자 가상담당자 미등록창고",
        "발주담당자 가상담당자 미등록창고 발주계산",
    ):
        parsed = resolve_io_nlq(query, today=date(2026, 10, 12))
        assert parsed["action"] == "발주 계산", query
        assert parsed["params"]["order_staff_nm"] == "가상담당자", query
        assert parsed["params"].get("_unconsumed_order_condition") == "미등록창고", query
    for query in (
        "발주계산 발주담당자 가상담당자 재고위치 본사창고",
        "발주담당자 가상담당자 발주계산 재고위치 본사창고",
    ):
        parsed = resolve_io_nlq(query, today=date(2026, 10, 12))
        assert parsed["action"] == "발주 계산", query
        assert parsed["params"]["order_staff_nm"] == "가상담당자", query
        assert parsed["params"].get("stock_nm_list") == ["본사창고"], query
        assert not parsed["params"].get("_unconsumed_order_condition"), query
        scoped, error, _ = resolve_order_stock_scope(
            parsed["params"], default_codes=["00001", "00247", "00901"],
            registered_names={"00001": "본사창고", "00247": "전주창고", "00901": "오토스토어"},
            saved_only=True,
        )
        assert not error and scoped["stock_cd_list"] == ["00001"], query
        assert scoped["_order_stock_scope_source"] == "explicit", query
    default = resolve_io_nlq("발주계산 발주담당자 가상담당자", today=date(2026, 10, 12))
    assert default["params"]["order_staff_nm"] == "가상담당자"
    assert not default["params"].get("stock_cd_list")
    assert not default["params"].get("_unconsumed_order_condition")
    default_scope, error, _ = resolve_order_stock_scope(
        default["params"], default_codes=["00001", "00247", "00901"], saved_only=True,
    )
    assert not error and default_scope["stock_cd_list"] == ["00001", "00247", "00901"]
    assert default_scope["_order_stock_scope_source"] == "company_default"
    for query, action in (
        ("현재고 조회 사용자명 김", "현재고 조회"),
        ("제품정보 조회 사용자코드 1", "제품정보 조회"),
    ):
        candidate = nlq_router.resolve_new_sims_nlq_candidate(query)
        assert candidate and candidate["action"] == action, query
        with patch.object(nlq_users, "_compute_dept_count_from_db", side_effect=AssertionError("DB")):
            assert not nlq_users.try_handle_users_nlq(
                query, room={"messages": []}, session_state={}, make_ts=lambda: "now",
                next_seq=lambda: 1, logger=logging.getLogger(__name__),
            ), query

    assert not nlq_users._has_user_master_anchor("SIMS 일일점점 재고위치 전주")
    options = pd.DataFrame([
        {"Rd01_Tcode": "00001", "Rd01_Hnm": "본사"},
        {"Rd01_Tcode": "00013", "Rd01_Hnm": "본사창고"},
    ])
    for query, expected in (
        ("사용자목록 재고위치 본사", "00001"),
        ("사용자목록 재고위치 본사창고", "00013"),
    ):
        stock_name = nlq_users._extract_stock_name(query)
        code, name = nlq_users._resolve_code_from_options(options, stock_name)
        assert code == expected and name == stock_name, query
        with (patch.object(nlq_router, "_try_handle_io_nlq", return_value=False),
              patch.object(nlq_users, "try_handle_users_nlq", return_value=True) as users):
            assert nlq_router.try_handle_nlq(
                query,
                room={"messages": [{"role": "assistant", "meta": {"action": "현재고 조회", "table_key": "old"}}]},
                session_state={"__sims_current_table_source_key": "old"}, make_ts=lambda: "now",
                next_seq=lambda: 1, logger=logging.getLogger(__name__),
            ), query
            users.assert_called_once()

    assert not is_order_calculation_request("발주 계산식")
    assert nlq_router.resolve_new_sims_nlq_candidate("발주 계산식") is None
    assert parse_business_help_knowledge_request("발주 계산식") is not None
    assert is_order_calculation_request("발주 계산해줘")
    print("PASS: NLQ intent, semantic parameters, and no-DB boundaries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Focused offline contracts for the October NLQ closeout."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.io_nlq import has_unconsumed_detail_condition, resolve_io_nlq
from app.sims.nlq.nlq_router import _extract_dashboard_nlq_conditions, _resolve_dashboard_nlq_action
from tools.check_current_table_source_date_authority import _dispatch
from tools.check_nlq_official_206_regression import _action_name_matches
from tools.check_nlq_regression_harness_alignment_20261006 import (
    FocusedCase, _followup_result_contract, _run_current_table,
)
from tools.check_nlq_golden_250_focused_smoke_20261007 import _current_table_classification


def main() -> int:
    for query in (
        "심스 일일점검", "심스 일일점검 제약사 삼진",
        "심스 일일점검 발주담당자 김", "SIMS 일일점검",
    ):
        assert _resolve_dashboard_nlq_action(query), query
        conditions, residual = _extract_dashboard_nlq_conditions(query)
        assert not residual.strip(), (query, residual)
        if "제약사" in query:
            assert conditions.get("제약사") == "삼진", conditions
        if "발주담당자" in query:
            assert conditions.get("발주담당자") == "김", conditions

    for query, action, product, vendor in (
        ("매입명세조회 매입처 온라인팜 제품 팔팔정", "입고명세 조회", "팔팔정", "온라인팜"),
        ("매출명세조회 매출처 소망 제품 낙소졸", "출고명세 조회", "낙소졸", "소망"),
    ):
        parsed = resolve_io_nlq(query)
        assert parsed and parsed["action"] == action, (query, parsed)
        assert parsed["params"].get("physic_nm") == product, (query, parsed)
        assert parsed["params"].get("ven_nm") == vendor, (query, parsed)
        assert not has_unconsumed_detail_condition(query, action, parsed["params"]), query

    assert _action_name_matches("계약단가 조회", "최종 계약단가 조회")

    parent_calls = 0
    original = None
    parent_payload = None
    class Router:
        def try_handle_nlq(self, _query, **kwargs):
            nonlocal parent_calls
            parent_calls += 1
            kwargs["session_state"]["__sims_current_table_source_key"] = "original"
            kwargs["session_state"]["__sims_current_table_source_action"] = "매입처별 재고부족 현황"
            kwargs["session_state"]["original_table"] = pd.DataFrame({"부족제품수": [3, 2]})
            capture.items.append({"action": "매입처별 재고부족 현황", "meta": {"result_status": "success", "source_call_count": 1}})
            return True

    def fake_namespace(state, _push):
        def followup(query, **_kwargs):
            assert "후속질문" not in state
            state["후속질문"] = query
            capture.items.append({
                "action": "현재표 TOP", "meta": {
                    "result_status": "success", "source_call_count": 0,
                    "source_table_key": "original", "source_action": "매입처별 재고부족 현황",
                },
            })
            return True
        return {"_try_handle_current_table_dataframe_followup": followup}

    contract = {"rank_contract_pass": True, "requested_metric": "", "requested_top": 10,
                "result_columns": "", "metric_order_pass": True}
    for query in (
        "현재표 부족제품수 top 10 보여줘", "현재표 관련제품수 top 10",
        "현재표 재고없음제품수 top 10", "현재표 음수재고제품수 top 10",
        "현재표 매입처원본재고금액 top 10",
    ):
        state = deepcopy(original) if original is not None else {}
        capture = SimpleNamespace(items=[], push=lambda **_kwargs: True)
        def remember(handled, payload, parent_state):
            nonlocal original, parent_payload
            original = deepcopy(parent_state)
            parent_payload = {"handled": handled, "payload": deepcopy(payload)}
        case = FocusedCase("synthetic", query, "매입처별 재고부족현황",
                           parent_question="매입처별 재고부족현황", parent_action="",
                           runtime_followup=query)
        with (patch("tools.check_nlq_regression_harness_alignment_20261006._main_namespace", fake_namespace),
              patch("tools.check_nlq_regression_harness_alignment_20261006._followup_result_contract", return_value=contract)):
            detail = _run_current_table(case, Router(), state, {}, capture, lambda: 1,
                                        cached_parent=parent_payload, parent_ready_hook=remember if original is None else None)
        assert detail["verdict"] == "PASS", detail
        assert state["후속질문"] == query
        assert "후속질문" not in original
    assert parent_calls == 1

    frame = pd.DataFrame({
        "제품코드": ["A", "B"], "제품명": ["품목A", "품목B"],
        "매입처명": ["매입A", "매입B"], "거래처명": ["매출A", "매출B"],
        "재고위치명": ["창고A", "창고B"],
        "수량": [2, 3], "공급가액": [200, 300], "세액": [20, 30],
        "합계금액": [220, 330], "출고일자": ["20261001", "20261002"],
    })
    for query, action, metric in (
        ("현재표 제품별 매입금액 TOP 20", "입고명세 조회", "매입금액"),
        ("현재표 제품별 매출금액 TOP 20", "출고명세 조회", "매출금액"),
        ("현재표 제품별 출고수량 TOP 20", "출고명세 조회", "출고수량"),
        ("현재표 매입처별 매출금액", "출고명세 조회", "매출금액"),
        ("현재표 재고위치별 매출금액", "출고명세 조회", "매출금액"),
    ):
        event, payload = _dispatch(query, frame, action)
        assert event == "table", (query, payload)
        result = payload["df"]
        assert metric in result.columns and 1 <= len(result) <= 20, (query, result.columns)
        assert payload.get("extra_meta", {}).get("source_call_count") == 0
    carry_only = pd.DataFrame({"입출고일자": ["이월재고"], "재고수량": [5], "제품명": ["품목A"]})
    event, payload = _dispatch("현재표 일별 집계", carry_only, "제품수불현황 조회")
    assert event == "notice" and payload["extra_meta"]["result_status"] == "no_data"
    assert not _followup_result_contract("현재표 일별 집계", {"action": "현재표 날짜 집계 결과 없음"})["rank_contract_pass"]
    assert _followup_result_contract(
        "현재표 일별 집계", {"df": pd.DataFrame({"일자": ["2026-10-01"], "건수": [1]})}
    )["rank_contract_pass"]
    assert _followup_result_contract(
        "현재표 제품별 매입금액 TOP 20",
        {"df": pd.DataFrame({"제품명": ["A", "B"], "매입금액": [300, 200]})},
    )["rank_contract_pass"]
    assert _current_table_classification({
        "verdict": "FAIL", "followup_status": "no_data", "date_columns": "입출고일자",
        "valid_date_rows": 0, "source_rows": 1, "current_table_extra_erp_source_call": 0,
    }) == "DATA_NO_RESULT_ALLOWED"
    print("PASS dashboard alias 4, IO alias 2, R070 compatibility, sibling parent once/restore 5, current-table grouping 5")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

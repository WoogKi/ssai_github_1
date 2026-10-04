"""Offline gates for order evidence, decimal boundaries and current-table facts."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import logging
from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
from app.services.order_calculation_contract import calculate_quantities
from app.services.order_calculation_service import (
    _infer_unit_from_windows, _order_reason, _unit_history_completeness,
    assemble_result, build_order_unit_history_params, build_pending_params,
)
from app.services.rddbc170_rddbc180_order_service import (
    _filters, get_expected_inbound_product_totals, get_order_df, normalize_order_params,
)
from app.sims.nlq.nlq_router import _try_handle_io_nlq
from app.ui.current_table_followups.action_dispatcher import build_current_table_interpretive_facts
from app.ui.current_table_followups.analysis_facts import fit_analysis_context
from app.ui.order_calculation_editor import apply_actual_edits
from tools.check_order_calculation_contract import fixture


def test_safety_boundary() -> None:
    params, sources = fixture()
    params.update(order_date="2026-09-03", target_days=15, closing_day=0)
    sources["business_dates"] = [
        date(2026, 9, 1) + timedelta(days=i) for i in range(30)
        if (date(2026, 9, 1) + timedelta(days=i)).weekday() < 5
    ]
    sources["demand"]["당월 현재출고수량"] = Decimal(1)
    sources["demand"]["수요예상기준"] = "비교자료부족"
    sources["elapsed_days"] = 3
    sources["normal_outbound_verified"] = True
    for stock, trigger in (("1", True), ("1.000000000000000000000000001", False),
                           ("0.999999999999999999999999999", True), ("0", True), ("-1", True)):
        sources["demand"]["현재재고수량"] = Decimal(stock)
        row = assemble_result(params, sources).iloc[0]
        assert row["안전재고 기준수량"] == 1
        assert bool(row["발주trigger"]) is trigger, (stock, row["안전재고 기준수량"])
        if stock == "1":
            assert row["적용 필요예정수량"] == 5
            assert row["계산 발주수량"] == 4 and row["추천 발주수량"] == 10
        if not trigger:
            assert row["추천 발주수량"] == 0
            assert "안전재고 초과" in row["발주사유/계산근거"]

    above = calculate_quantities(stock=Decimal(119), pending=Decimal(0),
        safety_demand=Decimal("36.6"), horizon_demand=Decimal(183))
    assert above["계산 발주수량"] == 64 and above["추천 발주수량"] == 0
    small = calculate_quantities(stock=Decimal(0), pending=Decimal(20),
        safety_demand=Decimal("4.05"), horizon_demand=Decimal("20.25"))
    assert small["계산 발주수량"] == Decimal("0.25") and small["추천 발주수량"] == 10
    exact = calculate_quantities(stock=Decimal(0), pending=Decimal(0),
        safety_demand=Decimal("13.2"), horizon_demand=Decimal(88) * 15 / 20,
        increasing=False)
    assert exact["계산 발주수량"] == 66 and exact["추천 발주수량"] == 60
    repeated = calculate_quantities(stock=Decimal(6), pending=Decimal(0),
        safety_demand=Decimal("11.80879120879121"),
        horizon_demand=Decimal("59.04395604395604"), unit=Decimal(10), increasing=True)
    assert repeated["추천 발주수량"] == 60
    assert "구색 확보 최소10" in _order_reason({
        **small, "계산상태": "발주해당", "발주단위": None,
        "안전재고 기준수량": Decimal("4.05"), "적용 필요예정수량": Decimal("20.25"),
        "재고수량": Decimal(0), "입고예정수량": Decimal(20), "추세판정": "유지",
    }, 15)


def test_period_and_router() -> None:
    today = date(2026, 10, 4)
    cases = (
        ("제품 리포에이 발주조회 2026", "리포에이", "20260101", "20261231"),
        ("제품명 리포에이 발주조회 2026", "리포에이", "20260101", "20261231"),
        ("제품명 리포에이 발주조회 202609", "리포에이", "20260901", "20260930"),
        ("2026-09-01~2026-09-30 제품명 리포에이정 발주조회", "리포에이정", "20260901", "20260930"),
        ("제품명 A2026정 발주조회 202609", "A2026정", "20260901", "20260930"),
    )
    from app.services import rddbc170_rddbc180_order_service as order_service
    real_service = order_service.get_order_result
    for question, name, start, end in cases:
        parsed = resolve_registered_erp_table_nlq(question, today=today)
        assert parsed["action"] == "발주조회"
        assert (parsed["params"]["physic_nm"], parsed["params"]["date_from"],
                parsed["params"]["date_to"]) == (name, start, end), parsed
        captured = []
        def service(params=None):
            q = params
            captured.append(dict(q))
            return real_service(q)
        with patch.object(order_service, "get_order_result", side_effect=service), \
             patch.object(order_service, "get_order_df", return_value=pd.DataFrame(columns=["제품코드"])), \
             patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda p, a: p.get("meta")):
            assert _try_handle_io_nlq(question, room={}, session_state={}, make_ts=lambda: "offline",
                                      next_seq=lambda: 1, logger=logging.getLogger("order-evidence-gate"))
        assert len(captured) == 1, (question, captured)
        assert (captured[0]["physic_nm"], captured[0]["date_from"], captured[0]["date_to"]) == (name, start, end)

    unlabeled = "리포에이정 발주조회 202609"
    parsed = resolve_registered_erp_table_nlq(unlabeled, today=today)
    assert parsed["params"]["_registered_unlabeled_entity"] == "리포에이정"
    assert parsed["params"]["date_from"] == "20260901"
    captured = []
    def service(params=None):
        q = params
        captured.append(dict(q))
        return real_service(q)
    with patch("app.services.io_nlq._lookup_unlabeled_io_entity_candidates",
               return_value=[{"match_type": "product", "match_value": "리포에이정", "match_code": "fixture"}]), \
         patch.object(order_service, "get_order_result", side_effect=service), \
         patch.object(order_service, "get_order_df", return_value=pd.DataFrame(columns=["제품코드"])), \
         patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda p, a: p.get("meta")):
        assert _try_handle_io_nlq(unlabeled, room={}, session_state={}, make_ts=lambda: "offline",
                                  next_seq=lambda: 1, logger=logging.getLogger("order-evidence-gate"))
    assert len(captured) == 1 and captured[0]["physic_nm"] == "리포에이정"
    assert (captured[0]["date_from"], captured[0]["date_to"]) == ("20260901", "20260930")


def test_source_scope_and_facts() -> None:
    base = {"policy_date": "20261006", "stock_apply_cd": "50001",
            "cost_apply_cd": "50002", "stock_cd_list": ["001"],
            "order_vendor_cd": "10025", "order_staff_nm": "scope",
            "_business_dates": ("20261006", "20261005", "20261002", "20261001")}
    pending = build_pending_params(base)
    unit = build_order_unit_history_params(base, reference=date(2026, 10, 6))
    assert "order_vendor_cd" not in pending and "order_vendor_cd" not in unit
    assert "order_staff_nm" in pending and "order_staff_nm" not in unit
    assert unit["date_from"] == "20260706" and unit["date_to"] == "20261006"
    assert unit["stock_apply_cd"] == pending["stock_apply_cd"]
    assert unit["cost_apply_cd"] == pending["cost_apply_cd"]
    clauses, binds = _filters(normalize_order_params(unit, mode="order"), mode="order")
    sql = " ".join(clauses)
    assert "D.Rd18_Or_Di IN" in sql and "D.Rd18_Stock_Apply_Cd = ?" in sql
    assert "20260706" in binds and "20261006" in binds
    pending_calls = []
    def pending_select(sql, values):
        pending_calls.append((sql, tuple(values)))
        return pd.DataFrame({"제품코드": ["fixture"], "입고예정수량": [10], "_공백재고위치행수": [0]})
    with patch("app.services.rddbc170_rddbc180_order_service.execute_bound_select",
               side_effect=pending_select):
        pending_result = get_expected_inbound_product_totals(pending)
    assert len(pending_calls) == 1 and pending_result.iloc[0]["입고예정수량"] == 10
    pending_sql, pending_binds = pending_calls[0]
    assert "D.Rd18_Quantity + D.Rd18_Oquantity - D.Rd18_In_Quantity" in pending_sql
    assert "D.Rd18_Or_Di IN ('1', '2')" in pending_sql
    assert all(day in pending_binds for day in base["_business_dates"])
    assert "D.Rd18_Stock_Apply_Cd = ?" in pending_sql
    unit_calls = []
    def unit_select(sql, values):
        unit_calls.append((sql, tuple(values)))
        return pd.DataFrame({
            "발주일자": ["20261001", "20261002", "20261006"],
            "발주거래처코드": ["10025"] * 3, "제품코드": ["fixture"] * 3,
            "발주수량": [Decimal(10), Decimal(10), Decimal(20)],
            "발주순번": [1, 2, 3], "상세순번": [1, 1, 1],
        })
    with patch("app.services.rddbc170_rddbc180_order_service.execute_bound_select",
               side_effect=unit_select):
        unit_result = get_order_df(unit, mode="order")
    assert len(unit_calls) == 1 and "D.Rd18_Quantity AS [발주수량]" in unit_calls[0][0]
    assert "D.Rd18_In_Quantity" not in unit_calls[0][0]
    assert "D.Rd18_Or_Di IN (?,?,?)" in unit_calls[0][0]
    assert _unit_history_completeness(unit_result, recent_start="20260906", top=100000) == (True, True)
    inferred, _, _ = _infer_unit_from_windows(unit_result.to_dict("records"),
        recent_start="20260906", period=["20260706", "20261006"],
        recent_complete=True, full_complete=True)
    assert inferred == 10
    duplicate = pd.concat([unit_result, unit_result.iloc[[0]]], ignore_index=True)
    assert _unit_history_completeness(duplicate, recent_start="20260906", top=100000) == (False, False)

    params, sources = fixture()
    row = assemble_result(params, sources).iloc[0]
    frame = pd.DataFrame([row])
    facts = build_current_table_interpretive_facts(df=frame, query="왜 이렇게 많이 나왔어?",
                                                    source_action="발주 계산")
    assert facts["status"] == "success", facts
    decision = facts["whole_table_facts"]["order_decision_facts"]
    for field in ("적용 필요예정수량", "재고수량", "입고예정수량", "계산 발주수량",
                  "발주단위 근거", "추천 발주수량", "실제 발주수량", "발주사유/계산근거"):
        assert field in decision, field
    assert "ERP 발주는 등록되지 않음" in facts["whole_table_facts"]["order_registration_status"]
    transmitted = fit_analysis_context({
        "analysis_target": "current_table_whole_facts", "whole_table_facts": facts["whole_table_facts"],
    }, 12000)
    assert transmitted["whole_table_facts"]["order_decision_facts"] == decision
    changed = apply_actual_edits(frame, frame, {0: {"실제 발주수량": "40"}})
    edited_facts = build_current_table_interpretive_facts(df=changed,
        query="왜 이렇게 많이 나왔어?", source_action="발주 계산")
    edited = edited_facts["whole_table_facts"]["order_decision_facts"]
    assert edited["실제 발주수량"] == "40"
    assert "사용자 입력 40" in edited["발주사유/계산근거"]


if __name__ == "__main__":
    test_safety_boundary()
    test_period_and_router()
    test_source_scope_and_facts()
    print("order evidence boundary: PASS (DB=0, LLM=0)")

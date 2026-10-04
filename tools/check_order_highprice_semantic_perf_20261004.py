"""Offline high-price, semantic-scope, and edited-facts integration gate."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.order_calculation_contract import calculate_quantities, quantity_adjustment
from app.services import order_calculation_service as order_service
from app.services import rddbc170_rddbc180_order_service as order_source
from app.ui.current_table_followups.analysis_facts import build_whole_table_facts
from app.ui.order_calculation_editor import apply_actual_edits
from tools.check_order_calculation_contract import fixture


def check_quantity_and_revision():
    for price, high in ((D("999999.99"), False), (D("1000000"), True),
                        (D("1000000.01"), True), (None, False), (D(0), False),
                        (D("NaN"), False), (D("Infinity"), False)):
        for unit in (None, D(1), D(5), D(12), D(100)):
            for increasing in (False, True):
                for raw in map(D, ("0", "0.16", "0.7", "1", "1.6", "4", "10", "19.59825")):
                    row = calculate_quantities(stock=D(0), pending=D(0), safety_demand=D(0),
                                               horizon_demand=raw, unit=unit, increasing=increasing,
                                               price=price)
                    applies = raw > 0 and high
                    assert row["고가 조정단위 적용"] is applies
                    assert row["기본 조정단위 적용"] is (raw > 0 and not high and unit is None)
                    if applies:
                        expected = (raw.to_integral_value(rounding="ROUND_CEILING") if increasing else
                                    max(D(1), raw.to_integral_value(rounding="ROUND_FLOOR")))
                        assert row["추천 발주수량"] == expected
                    if raw == 0:
                        assert row["수량조정정책"] == "미적용" and row["추천 발주수량"] == 0
    for raw, unit, increasing, expected in (("0.18500625", None, False, 1),
                                             ("10.9582875", 5, True, 11),
                                             ("19.59825", None, False, 19),
                                             ("0.16", 100, False, 1)):
        assert quantity_adjustment(D(raw), None if unit is None else D(unit),
                                   increasing=increasing, price=D(1000000))["추천 발주수량"] == expected
    low = calculate_quantities(stock=D(0), pending=D(0), safety_demand=D(0),
                               horizon_demand=D(765), unit=D(240), price=D("20478.9"))
    assert low["추천 발주수량"] == 720
    no_order = calculate_quantities(stock=D(119), pending=D(0), safety_demand=D("36.6"),
                                    horizon_demand=D(183), unit=D(100), price=D(1000000))
    assert no_order["추천 발주수량"] == 0 and no_order["수량조정정책"] == "미적용"
    incomplete = calculate_quantities(stock=D(0), pending=D(0), safety_demand=D(0),
                                      horizon_demand=D("0.16"), price=D(1000000),
                                      default_unit_allowed=False)
    assert incomplete["추천 발주수량"] == 0 and not incomplete["고가 조정단위 적용"]
    missing = calculate_quantities(stock=D(0), pending=None, safety_demand=None,
                                   horizon_demand=None, price=D(1000000))
    assert missing["추천 발주수량"] is None and missing["수량조정정책"] == "미적용"

    params, sources = fixture()
    sources["order_price_3m"] = {("00001", "00007"): D(1000000)}
    sources["order_price_1y"] = {}
    high_row = order_service.assemble_result(params, sources).iloc[0]
    assert high_row["고가 조정단위 적용"] and high_row["발주단가"] == 1000000
    assert high_row["계산 발주수량"] == 32 and high_row["추천 발주수량"] == 32
    assert "고가 정책으로 조정단위1 적용" in high_row["발주사유/계산근거"]
    sources["order_history_unit"] = pd.DataFrame([
        {"제품코드": "00001", "발주거래처코드": "00100", "발주일자": f"202609{day:02d}",
         "발주수량": D(quantity)}
        for day, quantity in ((20, 5), (19, 10), (18, 5))
    ])
    confirmed = order_service.assemble_result(params, sources).iloc[0]
    assert confirmed["발주단위"] == 5 and confirmed["추천 발주수량"] == 32
    assert confirmed["고가 조정단위 적용"] and "최소수량 반복" in str(confirmed["발주단위 근거"])
    assert "발주단위 5" in confirmed["발주사유/계산근거"]
    frame = pd.DataFrame([high_row])
    edited = apply_actual_edits(frame, frame, {0: {"실제 발주수량": "7"}})
    revised = edited.iloc[0]
    assert revised["추천 발주수량"] == 32 and revised["실제 발주수량"] == 7
    assert revised["추천대비수정수량"] == -25 and revised["발주금액(부가세포함)"] == D(7700000)
    facts = build_whole_table_facts(edited, action="발주계산", query="왜 7인가")
    decision = facts["order_decision_facts"]
    assert decision["수량조정정책"] == "고가 조정단위1"
    assert decision["실제 발주수량"] == "7" and decision["추천대비수정수량"] == "-25"
    assert "고가 정책으로 조정단위1 적용" in decision["발주사유/계산근거"]

    old = dict(high_row)
    for key in ("수량조정정책", "수량조정단위", "수량정수화", "최소수량 적용",
                "기본 조정단위 적용", "고가 조정단위 적용"):
        old.pop(key, None)
    old.update({"계산 발주수량": D(4), "추천 발주수량": D(4),
                "실제 발주수량": D(7), "추천대비수정수량": D(3)})
    assert "기본 조정단위10" not in order_service._order_reason(old, 15)


def check_source_scope():
    params = {"company_id": 3, "order_date": "2026-10-06", "target_days": 15,
              "product_prescription_semantic": "prescription"}
    master = pd.DataFrame({"제품코드": ["10001", "10002"]})
    supplier = pd.DataFrame({"product_code": ["10001", "10002"]})
    demand = pd.DataFrame({"제품코드": ["10001", "10002"]})
    unit_source = pd.DataFrame([
        {"제품코드": code, "발주일자": "20260920", "발주거래처코드": "V1",
         "발주순번": seq, "상세순번": "1", "발주수량": D(qty)}
        for seq, (code, qty) in enumerate((("10001", 5), ("10002", 12), ("90000", 100)), 1)
    ])
    unit_source.attrs["order_history_query_mode"] = "unit_inference_r170_r180_minimal"
    pending_source = pd.DataFrame({"제품코드": ["10001", "10002", "90000"],
                                   "입고예정수량": [D(2), D(3), D(99)]})
    customer_seen = []
    unit_seen = []
    pending_seen = []
    def customer(query):
        customer_seen.append(query)
        return pd.DataFrame({"제품코드": ["10001", "10002"], "거래처수": [1, 2]})
    def unit(query, *, mode):
        unit_seen.append((query, mode))
        return unit_source.copy()
    def pending(query):
        pending_seen.append(query)
        return pending_source.copy()
    scope = SimpleNamespace(stock_codes=(), product_group_codes=(), product_di_codes=(),
                            product_class_codes=(), io_gu_codes=("500",), stock_mode="real")
    with ExitStack() as stack:
        # Any missed mock must fail before a physical connection attempt.
        stack.enter_context(patch("sqlalchemy.engine.Engine.connect", side_effect=AssertionError("DB connection forbidden")))
        stack.enter_context(patch("pyodbc.connect", side_effect=AssertionError("DB connection forbidden")))
        stack.enter_context(patch("app.services.business_calendar_service.next_business_day_on_or_after",
                                  return_value=SimpleNamespace(status="ready", effective_date="20261006",
                                                               shifted=False, shift_days=0, authority="fixture")))
        stack.enter_context(patch("app.services.dashboard_inventory_frequency_snapshot_service.resolve_dashboard_profile_stock_scope", return_value=scope))
        stack.enter_context(patch("app.services.ssai_analysis_profile_service.load_dashboard_profile_checked",
                                  return_value=SimpleNamespace(profile={})))
        stack.enter_context(patch("app.services.ssai_analysis_profile_service.normalize_company_default_conditions",
                                  return_value={"major_purchase_vendor_days": 90}))
        stack.enter_context(patch("app.services.snapshot_product_information_service.get_snapshot_product_information_result",
                                  return_value={"meta": {"snapshot_status": "ready"}, "df": master}))
        stack.enter_context(patch("app.services.dashboard_inbound_facts_service.get_dashboard_inbound_scope_authority",
                                  return_value=supplier))
        stack.enter_context(patch("app.services.analytics_sales_trend_service.get_stock_shortage_df", return_value=demand))
        stack.enter_context(patch("app.services.analytics_sales_trend_service.get_outbound_customer_counts_df", side_effect=customer))
        stack.enter_context(patch("app.services.rddbc170_rddbc180_order_service.get_order_df", side_effect=unit))
        stack.enter_context(patch("app.services.rddbc170_rddbc180_order_service.get_expected_inbound_product_totals", side_effect=pending))
        stack.enter_context(patch("app.services.ssai_business_calendar_repository.load_official_holidays",
                                  return_value=SimpleNamespace(status="ready", holiday_dates=frozenset())))
        stack.enter_context(patch("app.services.business_calendar_service.business_day_month_context",
                                  return_value=SimpleNamespace(elapsed_business_days=4)))
        result = order_service.load_sources(params)
    assert len(customer_seen) == len(unit_seen) == len(pending_seen) == 1
    assert customer_seen[0]["order_product_code_list"] == ["10001", "10002"]
    assert "product_prescription_semantic" not in customer_seen[0]
    assert "product_prescription_semantic" not in unit_seen[0][0]
    assert "product_prescription_semantic" not in pending_seen[0]
    assert pending_seen[0]["pending_product_code_list"] == ["10001", "10002"]
    assert unit_seen[0][0]["order_unit_product_code_list"] == ["10001", "10002"]
    assert unit_seen[0][0]["_order_unit_selected_scope"] is True
    assert order_source._use_order_unit_history_minimal(order_source.normalize_order_params(unit_seen[0][0], mode="order"))
    assert set(result["order_history_unit"]["제품코드"]) == {"10001", "10002"}
    assert set(result["pending"]["제품코드"]) == {"10001", "10002"}
    expected_unit = unit_source.loc[unit_source["제품코드"].isin({"10001", "10002"})].copy()
    expected_pending = pending_source.loc[pending_source["제품코드"].isin({"10001", "10002"})].copy()
    pd.testing.assert_frame_equal(result["order_history_unit"].reset_index(drop=True),
                                  expected_unit.reset_index(drop=True))
    pd.testing.assert_frame_equal(result["pending"].reset_index(drop=True),
                                  expected_pending.reset_index(drop=True))
    assert result["order_history_complete"] and result["current_customer_counts"] == {"10001": 1, "10002": 2}
    assert result["diagnostic_stage_evidence"]["order_history_unit_source"]["source_rows"] == 3
    assert result["diagnostic_stage_evidence"]["pending_four_business_days"]["source_rows"] == 3
    optimized = order_source.normalize_order_params(unit_seen[0][0], mode="order")
    legacy = order_source.normalize_order_params(
        {**unit_seen[0][0], "_order_unit_selected_scope": False,
         "product_prescription_semantic": "prescription"}, mode="order"
    )
    assert order_source._use_order_unit_history_minimal(optimized)
    assert not order_source._use_order_unit_history_minimal(legacy)
    optimized_clauses, optimized_binds = order_source._filters(optimized, mode="order")
    legacy_clauses, legacy_binds = order_source._filters(legacy, mode="order")
    assert "Rddbc046" not in " ".join(optimized_clauses)
    assert "Rddbc046" in " ".join(legacy_clauses)
    assert optimized_binds[:-1] == legacy_binds
    assert "<codes>" in optimized_binds[-1]
    source_complete = order_service._unit_history_completeness(unit_source.head(2),
                                                                 recent_start="20260906", top=2)
    filtered_complete = order_service._unit_history_completeness(expected_unit.head(1),
                                                                   recent_start="20260906", top=2)
    assert not source_complete[1] and filtered_complete[1]
    sql_seen = []
    def order_select(sql, binds):
        sql_seen.append((sql, binds))
        return unit_source.copy()
    with patch.object(order_source, "execute_bound_select", side_effect=order_select):
        sql_result = order_source.get_order_df(unit_seen[0][0], mode="order")
    assert len(sql_seen) == 1 and "SELECT TOP" not in sql_seen[0][0]
    assert "Rddbc040" not in sql_seen[0][0] and "Rddbc046" not in sql_seen[0][0]
    assert ".nodes('/codes/c')" in sql_seen[0][0] and len(sql_seen[0][1]) < 20
    assert sql_result.attrs["order_unit_history_complete_scope"] is True
    assert order_service._unit_history_completeness(sql_result, recent_start="20260906", top=2) == (True, True)
    from app.services import analytics_sales_trend_service as sales_source
    customer_sql = []
    with patch.object(sales_source, "query_to_df", side_effect=lambda sql, query: (
        customer_sql.append((sql, query)) or pd.DataFrame({"제품코드": ["10001"], "거래처수": [1]})
    )):
        sales_source.get_outbound_customer_counts_df(customer_seen[0])
    assert len(customer_sql) == 1 and "product_prescription_semantic" not in customer_sql[0][1]
    assert customer_sql[0][1]["order_product_code_list"] == ["10001", "10002"]
    pending_sql = []
    with patch.object(order_source, "execute_bound_select", side_effect=lambda sql, binds: (
        pending_sql.append((sql, binds)) or pending_source.copy()
    )), patch.object(order_source, "_expected_business_dates", return_value=(
        "20261001", "20261002", "20261005", "20261006"
    )):
        order_source.get_expected_inbound_product_totals(pending_seen[0])
    assert len(pending_sql) == 1 and "PrescriptionStd" not in pending_sql[0][0]
    assert "GROUP BY D.Rd18_Physic_Cd" in pending_sql[0][0]
    assert "D.Rd18_Physic_Cd IN (?,?)" in pending_sql[0][0]


if __name__ == "__main__":
    check_quantity_and_revision()
    check_source_scope()
    print("PASS high-price quantity, edited facts, semantic source handoff; DB connections=0")

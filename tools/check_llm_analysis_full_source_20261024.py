"""DB-free regression for full-source IO analysis and verified final display."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import (
    _build_full_source_analysis_ctx, _build_sims_analysis_context_from_df,
)
import app.ui.chat_middleware as chat_middleware
from app.ui.current_table_followups.analysis_facts import (
    build_verified_group_table, build_verified_whole_table,
)
from tools.check_llm_analysis_final_20261023 import _model_payload
from tools.check_llm_analysis_verified_output_20261022 import saved_message


def outbound_frame(rows: int, groups: int = 17) -> pd.DataFrame:
    return pd.DataFrame({
        "출고일자": ["20261023"] * rows,
        "제품코드": [f"{index:05d}" for index in range(rows)],
        "거래처명": [f"거래처{index % groups}" for index in range(rows)],
        "수량": [index % 7 + 1 for index in range(rows)],
        "공급가액": [1000 + index % 100 for index in range(rows)],
        "세액": [100 + index % 10 for index in range(rows)],
    })


def test_full_source() -> None:
    frame = outbound_frame(7113)
    meta = {
        "display_row_count": 200, "row_count_total": 200,
        "out_detail_summary": {
            "row_count": 200, "row_count_total": 200,
            "summary_basis": "표시된 결과 기준", "qty_sum": 922,
        },
        "llm_summary_md": "전체 200건, 수량 합계 922개",
        "download_limit_rows": 100000,
    }
    params = {"date_from": "20261023", "date_to": "20261023"}
    display_only = _build_sims_analysis_context_from_df(
        frame.head(200), result={}, action_name="출고명세 조회",
        params=params, meta=meta,
    )
    assert display_only["row_count"] == 200
    assert display_only["whole_table_facts"]["analysis_scope"] == "display_only"
    assert not display_only["detail_summary"] and not display_only["llm_summary_md"]
    assert "analysis_row_count" not in display_only["meta"]
    assert "row_count_total_for_analysis" not in display_only["meta"]
    context = _build_full_source_analysis_ctx(
        frame, action="출고명세 조회", params=params,
        meta=meta, display_rows=200,
    )
    context["table_key"] = "outbound-7113"
    assert context["row_count"] == 7113
    assert context["display_row_count"] == 200
    assert context["analysis_scope"] == "full_source"
    assert not context["detail_summary"] and not context["llm_summary_md"]
    previous_st = chat_middleware.st
    chat_middleware.st = SimpleNamespace(session_state={})
    try:
        chat_middleware._cache_sims_analysis_ctx_by_table_key(context)
        selected, source = chat_middleware._select_sims_analysis_ctx_for_table(
            table_key="outbound-7113", action="출고명세 조회")
        assert source == "cache" and selected["row_count"] == 7113
        assert selected["whole_table_facts"]["row_count"] == 7113
    finally:
        chat_middleware.st = previous_st
    delivered = _model_payload(context, "어제 출고현황 분석")
    facts = delivered["whole_table_facts"]
    assert facts["row_count"] == 7113 and facts["group_count"] == 17
    assert facts["numeric_metrics"]["수량"]["sum"] == frame["수량"].sum()
    assert facts["numeric_metrics"]["공급가액"]["sum"] == frame["공급가액"].sum()
    assert "20261023" in delivered["query_scope_summary"]
    assert not build_verified_group_table(facts)
    table = build_verified_whole_table(
        facts, scope=delivered["query_scope_summary"], display_rows=200)
    assert "전체 7,113건" in table and "화면 표시 200건" in table
    assert "그룹별 수치의 완전한 검증표가 없어" in table
    assert "| 수량 | 28,449 |" in table
    result = saved_message(table, facts=facts, model_text=(
        "1. 출고 현황\n전체 7,113건입니다.\n"
        "수량 합계는 99,999개입니다.\n"
        "거래처0이 가장 높은 매출처입니다.\n---\n"
        "2. 주요 항목별 분석\n---\n3. 확인 사항\n"
        "상세 거래 근거를 확인하세요."
    ))["content"]
    assert "7,113" in result and "200건" in result
    assert "99,999개" not in result and "가장 높은" not in result
    assert "\n2. 확인 사항" in result and "\n3." not in result and "해석 생성 중" not in result
    assert "주요 항목별 분석" not in result and "\n---\n" not in result, result
    assert "전체 200건" not in result
    print("PASS outbound 7113/200 full source -> model -> verified screen/save")


def test_incomplete_source() -> None:
    frame = outbound_frame(200)
    meta = {"row_count_total": 7113, "download_limit_rows": 200,
            "download_source_status": "partial_limit"}
    context = _build_full_source_analysis_ctx(
        frame, action="출고명세 조회", params={"date_from": "20261023"},
        meta=meta, display_rows=200,
    )
    context["table_key"] = "outbound-partial-200"
    assert context["analysis_scope"] == "partial_source"
    delivered = _model_payload(context, "어제 출고현황 분석")
    facts = delivered["whole_table_facts"]
    assert facts["analysis_scope"] == "partial_source"
    table = build_verified_whole_table(facts, display_rows=200)
    assert "확보된 200건" in table and "전체 조회의 합계는 검증되지" in table
    result = saved_message(table, facts=facts, model_text="전체 200건입니다. 원천을 확인하세요.")["content"]
    assert "확보된 200건" in result and "전체 조회의 합계는 검증되지" in result, result
    assert "전체 200건입니다" not in result
    print("PASS partial source 200/7113 is not claimed as whole")


def test_inbound_929() -> None:
    rows = 929
    frame = pd.DataFrame({
        "입고일자": ["20261023"] * rows,
        "매입처명": [f"매입처{index % 17}" for index in range(rows)],
        "입고수량": [index % 9 + 1 for index in range(rows)],
        "최종공급가액": [2000 + index for index in range(rows)],
    })
    context = _build_full_source_analysis_ctx(
        frame, action="입고명세 조회", params={"date_from": "20261023"},
        meta={"row_count_total": rows, "display_row_count": 200},
        display_rows=200,
    )
    context["table_key"] = "inbound-929"
    delivered = _model_payload(context, "어제 입고현황 분석")
    facts = delivered["whole_table_facts"]
    assert facts["row_count"] == rows and facts["group_count"] == 17
    assert not build_verified_group_table(facts)
    table = build_verified_whole_table(facts, display_rows=200)
    assert "전체 929건" in table and "화면 표시 200건" in table
    assert f"{frame['입고수량'].sum():,}" in table
    final = saved_message(table, facts=facts, model_text=(
        "1. 입고 현황\n전체 929건입니다.\n2.\n원천 거래를 확인하세요."
    ))["content"]
    assert "전체 929건" in final and "\n2." not in final
    print("PASS inbound 929/200 verified whole totals")


if __name__ == "__main__":
    test_full_source()
    test_incomplete_source()
    test_inbound_929()

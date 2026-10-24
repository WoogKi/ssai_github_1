"""DB-free regression for large SIMS analysis contexts and verified prose."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import _build_sims_analysis_context_from_df
from app.ui.current_table_followups.analysis_facts import (
    build_whole_table_facts, compose_verified_analysis_output, fit_analysis_context,
)
from tools.check_llm_analysis_context_fix_20261022 import runtime
from tools.check_llm_analysis_verified_output_20261022 import saved_message


def _model_payload(ctx: dict, question: str, limit: int = 12000) -> dict:
    env, state, warnings, calls = runtime(16384 if limit == 12000 else 12288)
    env["_run_sims_llm_analysis_from_fragment"](
        question, analysis_ctx_override=ctx,
        clicked_table_key=ctx["table_key"], clicked_action=ctx["action"],
    )
    assert not warnings and len(calls) == 1, (warnings, state.get("__sims_analysis_attach_error"))
    system = calls[0]["messages_for_ai"][0]["content"]
    raw = system.split("[SIMS_JSON]\n", 1)[1].split("\n[/SIMS_JSON]", 1)[0]
    assert len(raw) <= limit
    return json.loads(raw)


def _inbound_context() -> dict:
    rows = 765
    frame = pd.DataFrame({
        "입고일자": ["20261023"] * rows,
        "제품코드": [f"{index:05d}" for index in range(rows)],
        "제품명": [f"제품{index}" for index in range(rows)],
        "매입처명": [f"매입처{index % 10}" for index in range(rows)],
        "입고수량": [index % 15 + 1 for index in range(rows)],
        "최종공급가액": [1000 + index for index in range(rows)],
        "최종세액": [100 + index % 10 for index in range(rows)],
        "입고금액": [1100 + index + index % 10 for index in range(rows)],
    })
    for index in range(67 - len(frame.columns)):
        frame[f"참고항목{index}"] = f"참고{index}"
    top = [{"name": f"매입처{index}", "qty_sum": 100 + index,
            "supply_sum": 10000 + index, "tax_sum": 1000 + index}
           for index in range(10)]
    detail = {
        "row_count": rows, "row_count_total": rows, "display_row_count": 200,
        "summary_basis": "전체 조회조건 기준", "query_summary": "20261023 입고현황",
        "qty_sum": int(frame["입고수량"].sum()),
        "supply_sum": int(frame["최종공급가액"].sum()),
        "tax_sum": int(frame["최종세액"].sum()),
        "amount_sum": int(frame["입고금액"].sum()),
        "vendor_count": 10, "product_count": rows,
        "top_purchase_vendors": top, "top_vendors": top,
        "top_products": [dict(item) for item in top],
        "top_stock_locations": [dict(item) for item in top],
    }
    params = {
        "date_from": "20261023", "date_to": "20261023",
        "month_from": "202610", "month_to": "202610",
        "stock_cd_list": ["00001", "00247", "00901"],
        "stock_cds": ["00001", "00247", "00901"],
        "_period_source_policy": {"default_policy": "today", "source_mode": "erp",
                                  "diagnostic_trace": ["stage" + str(i) for i in range(700)]},
    }
    meta = {"in_detail_summary": detail, "llm_summary_md":
            "조회조건: 20261023 입고현황\n" + "\n".join(
                f"- 매입처{index}: {10000 + index}원" for index in range(30)),
            "display_row_count": 200, "analysis_row_count": rows}
    ctx = _build_sims_analysis_context_from_df(
        frame, result={}, action_name="입고명세 조회", params=params, meta=meta)
    ctx["table_key"] = "inbound-fixture"
    return ctx


def test_inbound_and_sales() -> None:
    ctx = _inbound_context()
    original = deepcopy(ctx)
    before = len(json.dumps(ctx, ensure_ascii=False, default=str, separators=(",", ":")))
    delivered = _model_payload(ctx, "입고현황의 금액과 매입처를 분석해줘")
    after = len(json.dumps(delivered, ensure_ascii=False, default=str, separators=(",", ":")))
    assert ctx == original
    assert delivered["row_count"] == 765 and delivered["display_row_count"] == 200
    assert delivered["params"]["date_from"] == "20261023"
    assert delivered["params"]["stock_cd_list"] == ["00001", "00247", "00901"]
    assert delivered["detail_summary"]["amount_sum"] == original["detail_summary"]["amount_sum"]
    assert delivered["detail_summary"].get("top_purchase_vendors")
    assert delivered["whole_table_facts"]["group_consistency_pass"] is True
    print(f"PASS inbound synthetic context {before}->{after} chars")

    # The observed inbound failure retained about 9 KB of transport params
    # after samples were removed. Keep a similarly large set of applied codes.
    crowded = _inbound_context()
    crowded["params"]["product_group_list"] = [f"{index:05d}" for index in range(850)]
    crowded["params"]["product_di_list"] = [f"{index:04d}" for index in range(100)]
    crowded_before = len(json.dumps(crowded, ensure_ascii=False, default=str, separators=(",", ":")))
    crowded_payload = _model_payload(crowded, "입고현황의 금액과 매입처를 분석해줘")
    assert crowded_payload["detail_summary"]["amount_sum"] == crowded["detail_summary"]["amount_sum"]
    assert crowded_payload["row_count"] == 765
    omitted = crowded_payload["delivery_scope"]["applied_filter_values_omitted"]
    assert omitted["product_group_list"] == 850
    assert "product_di_list" not in omitted or omitted["product_di_list"] == 100
    assert "product_group_list" not in crowded_payload["params"]
    assert crowded["params"]["product_group_list"][0] == "00000"
    crowded_after = len(json.dumps(crowded_payload, ensure_ascii=False, default=str, separators=(",", ":")))
    print(f"PASS crowded inbound synthetic context {crowded_before}->{crowded_after} chars")

    protected = deepcopy(crowded)
    protected["params"]["product_group_list"] = [f"{index:05d}" for index in range(1200)]
    try:
        fit_analysis_context(protected, 12000, question="적용된 제품그룹 코드를 모두 알려줘")
    except ValueError as exc:
        assert str(exc) == "context_budget_exceeded"
    else:
        raise AssertionError("An exact filter-list question must not use omitted values")

    frame = pd.DataFrame({
        "제품코드": [f"{i:05d}" for i in range(10020)],
        "제품명": [f"품목{i}" for i in range(10020)],
        "추세판정": [["증가", "유지", "감소"][i % 3] for i in range(10020)],
        "출고빈도등급": ["A"] * 10020,
        "총매출액": [1000 + i % 100 for i in range(10020)],
        "총매출세액": [100 + i % 10 for i in range(10020)],
        "총출고수량": [i % 12 for i in range(10020)],
    })
    for index in range(46 - len(frame.columns)):
        frame[f"참고항목{index}"] = "값"
    sales = _build_sims_analysis_context_from_df(
        frame, result={}, action_name="품목별 매출 추세 요약표",
        params={"date_from": "20260401", "date_to": "20260930",
                "stock_cd_list": ["00001", "00247", "00901"],
                "product_group_list": ["0004:1", "0004:2"],
                "product_di_list": ["A", "B"],
                "product_class_list": ["1", "2"],
                "io_gu_list": [f"{index:03d}" for index in range(35)]},
        meta={"display_row_count": 300, "analysis_row_count": 10020})
    sales["table_key"] = "sales-trend-fixture"
    result = _model_payload(sales, "매출 합계와 추세판정의 관계를 알려줘")
    assert result["row_count"] == 10020 and result["display_row_count"] == 300
    assert result["whole_table_facts"]["group_column"] == "추세판정"
    assert "20260401" in result["query_scope_summary"]
    assert "재고위치" in result["query_scope_summary"]
    assert "제품그룹 2개 적용" in result["query_scope_summary"]
    assert "제품구분 2개 적용" in result["query_scope_summary"]
    assert "제품분류 2개 적용" in result["query_scope_summary"]
    assert result["whole_table_facts"]["numeric_metrics"]["총매출액"]["sum"] == int(frame["총매출액"].sum())
    assert result["whole_table_facts"]["numeric_metrics"]["총매출액"].get("group_values_complete") is True
    print("PASS 10,020-row sales trend synthetic context")


def test_verified_sentences() -> None:
    frame = pd.DataFrame({"추세판정": ["증가", "유지"],
                          "매출합계": [1000, 2000], "매출세액": [100, 200]})
    facts = build_whole_table_facts(frame, action="매출현황", query="매출합계 매출세액",
                                    group_column="추세판정", complete_groups=True)
    text = "1. 매출 현황\n매출 합계는 3,000원이고 매출세액은 300원입니다.\n2. 오류\n매출 합계는 9,999원입니다."
    result = compose_verified_analysis_output("검증표", text, facts=facts)
    assert "3,000원" in result and "300원" in result
    assert "9,999원" not in result and "2. 오류" not in result
    alias_frame = frame.rename(columns={"매출합계": "총매출액", "매출세액": "총매출세액"})
    alias_facts = build_whole_table_facts(alias_frame, action="매출현황", query="매출 합계",
                                          group_column="추세판정", complete_groups=True)
    alias_output = compose_verified_analysis_output(
        "검증표", "매출 합계는 3,000원이고 매출 세액은 300원입니다. 매출 합계는 9,999원입니다.",
        facts=alias_facts)
    assert "3,000원" in alias_output and "300원" in alias_output
    assert "9,999원" not in alias_output
    assert "\n2.\n" not in compose_verified_analysis_output("검증표", "1. 현황\n설명입니다.\n2.\n3. 관리 제안\n확인하세요.", facts=facts)
    saved = saved_message("검증표", facts=facts, model_text="매출 합계는 3,000원입니다.")
    assert "해석 생성 중" not in saved["content"]
    print("PASS verified spaced sales metrics and empty heading removal")


if __name__ == "__main__":
    test_inbound_and_sales()
    test_verified_sentences()

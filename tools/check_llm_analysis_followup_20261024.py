"""DB-free regression for SIMS analysis transport and unsupported group claims."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import (
    _analysis_query_params_from_payload, _build_sims_analysis_context_from_df,
)
from app.ui.current_table_followups.analysis_facts import (
    build_verified_group_table, build_whole_table_facts,
    compose_verified_analysis_output, fit_analysis_context,
)
from tools.check_llm_analysis_final_20261023 import _model_payload
from tools.check_llm_analysis_verified_output_20261022 import saved_message


def test_inbound_transport() -> None:
    rows = 929
    frame = pd.DataFrame({
        "입고일자": ["20261023"] * rows,
        "제품코드": [f"{index:05d}" for index in range(rows)],
        "제품명": [f"제품{index}" for index in range(rows)],
        "매입처명": [f"거래처{index % 17}" for index in range(rows)],
        "입고수량": [index % 11 + 1 for index in range(rows)],
        "최종공급가액": [index + 1000 for index in range(rows)],
        "최종세액": [index % 10 + 100 for index in range(rows)],
    })
    for index in range(67 - len(frame.columns)):
        frame[f"참고항목{index}"] = [f"참고{index}-{row}" for row in range(rows)]
    detail = {
        "row_count": rows, "row_count_total": rows, "display_row_count": 200,
        "query_summary": "2026-10-23 입고현황", "qty_sum": int(frame["입고수량"].sum()),
        "supply_sum": int(frame["최종공급가액"].sum()),
        "tax_sum": int(frame["최종세액"].sum()),
        "amount_sum": int(frame["최종공급가액"].sum() + frame["최종세액"].sum()),
        "top_purchase_vendors": [{"name": f"거래처{i}", "qty": i, "amount": i * 1000}
                                 for i in range(80)],
    }
    meta = {"in_detail_summary": detail, "display_row_count": 200,
            "analysis_row_count": rows,
            "llm_summary_md": "입고 분석 근거 " + "요약" * 2400}
    params = {"date_from": "20261023", "date_to": "20261023",
              "month_from": "202610", "month_to": "202610"}
    payload = {"params": params, "meta": meta}
    assert _analysis_query_params_from_payload(payload) == params
    assert _analysis_query_params_from_payload({"meta": {"params": params}}) == params
    assert _analysis_query_params_from_payload({"meta": meta}) == {}
    legacy = _build_sims_analysis_context_from_df(
        frame, result={}, action_name="입고명세 조회", params=meta, meta=meta)
    legacy_diagnostics = {}
    try:
        fit_analysis_context(legacy, 12000, question="입고현황의 합계와 매입처를 분석해줘",
                             diagnostics=legacy_diagnostics)
    except ValueError as exc:
        assert str(exc) == "context_budget_exceeded"
    else:
        raise AssertionError("Legacy meta-as-params path unexpectedly fit")
    assert legacy_diagnostics["section_chars_after"]["params"] > 8000
    context = _build_sims_analysis_context_from_df(
        frame, result={}, action_name="입고명세 조회",
        params=_analysis_query_params_from_payload(payload), meta=meta)
    context["table_key"] = "synthetic-inbound-929"
    before = len(json.dumps(context, ensure_ascii=False, default=str, separators=(",", ":")))
    delivered = _model_payload(context, "입고현황의 합계와 매입처를 분석해줘")
    assert delivered["params"] == params
    assert delivered["row_count"] == rows and delivered["display_row_count"] == 200
    assert delivered["detail_summary"]["qty_sum"] == detail["qty_sum"]
    assert delivered["detail_summary"]["amount_sum"] == detail["amount_sum"]
    facts = delivered["whole_table_facts"]
    assert facts["group_count"] == 17
    assert not any(stats.get("group_values_complete") for stats in facts["numeric_metrics"].values())
    assert not build_verified_group_table(facts)
    source_text = (
        f"1. 전체 현황\n전체 {rows:,}건입니다.\n"
        f"입고수량 합계는 {detail['qty_sum']:,}개입니다.\n"
        f"최종공급가액 합계는 {detail['supply_sum']:,}원입니다.\n"
        f"최종세액 합계는 {detail['tax_sum']:,}원입니다.\n"
        "입고수량 합계는 99,999개입니다.\n"
        "거래처0 최종공급가액은 99,999,999원입니다.\n"
        "거래처0의 최종공급가액 집중도가 가장 높습니다.\n2.\n"
        "원천 거래를 확인하세요."
    )
    saved = saved_message("", facts=facts, model_text=source_text)
    final = saved["content"]
    for value in (f"전체 {rows:,}건", f"{detail['qty_sum']:,}개",
                  f"{detail['supply_sum']:,}원", f"{detail['tax_sum']:,}원"):
        assert value in final, value
    assert "99,999개" not in final and "99,999,999원" not in final
    assert "집중도가 가장 높" not in final and "\n2." not in final
    assert "해석 생성 중" not in final
    after = len(json.dumps(delivered, ensure_ascii=False, default=str, separators=(",", ":")))
    assert after <= 12000
    print(f"PASS synthetic inbound 929/200/67, 17 vendors: context {before}->{after} chars, saved totals")


def test_generic_code_request() -> None:
    from tools.check_llm_analysis_final_20261023 import _inbound_context

    context = _inbound_context()
    context["params"]["product_group_list"] = [f"{i:05d}" for i in range(1200)]
    diagnostics = {}
    try:
        fit_analysis_context(context, 12000, question="적용된 코드를 모두 알려줘",
                             diagnostics=diagnostics)
    except ValueError as exc:
        assert str(exc) == "context_budget_exceeded"
    else:
        raise AssertionError("Required code values were silently omitted")
    assert "filter_values_product_group_list" not in diagnostics.get("reductions", [])
    assert diagnostics["params_shape"]["product_group_list"]["count"] == 1200
    print("PASS generic applied-code request fails closed")


def test_unverified_groups() -> None:
    frame = pd.DataFrame({"거래처명": [f"그룹{i}" for i in range(21)],
                          "매출금액": [1000 + i for i in range(21)]})
    facts = build_whole_table_facts(frame, action="매출현황", query="",
                                    group_column="거래처명", complete_groups=True)
    assert facts["group_distribution_omitted_count"] == 1
    table = build_verified_group_table(facts)
    assert not table
    rendered = compose_verified_analysis_output(
        table, "1. 현황\n매출금액은 99,999원입니다.\n2.\n그룹1에 매출이 집중됐습니다.\n원천 거래를 확인하세요.", facts=facts)
    assert "99,999원" not in rendered and "\n2." not in rendered
    assert "집중됐습니다" not in rendered
    assert "원천 거래를 확인하세요" in rendered
    assert "|" not in rendered
    saved = saved_message(
        "", facts=facts,
        model_text="1. 현황\n전체 21건입니다.\n매출금액 합계는 21,210원입니다.\n"
                   "그룹0 매출금액 99,999원입니다.\n2.\n원천 거래를 확인하세요.")
    assert "99,999원" not in saved["content"] and "\n2." not in saved["content"]
    assert "전체 21건" in saved["content"] and "21,210원" in saved["content"]
    assert "해석 생성 중" not in saved["content"]
    zero_frame = pd.DataFrame({"거래처명": [f"거래처{i}" for i in range(13)],
                               "매출금액": [0] * 13})
    zero_facts = build_whole_table_facts(zero_frame, action="매출현황", query="",
                                         group_column="거래처명", complete_groups=True)
    zero_output = compose_verified_analysis_output(
        "", "전체 13건이며 매출금액 합계는 0원입니다. 거래처0 매출금액은 0원입니다.",
        facts=zero_facts)
    assert "매출금액 합계는 0원" in zero_output
    assert "거래처0 매출금액은 0원" not in zero_output
    print("PASS 21 groups without verified table suppress unsupported claims")


def test_sales_scope_and_amount() -> None:
    rows = 10020
    frame = pd.DataFrame({
        "제품코드": [f"{i:05d}" for i in range(rows)],
        "총출고수량": [i % 8 for i in range(rows)],
        "총출고할증수량": [0] * rows,
        "총매출액": [1000 + i % 10 for i in range(rows)],
        "추세판정": [["증가", "감소", "유지"][i % 3] for i in range(rows)],
        "출고빈도등급": ["A"] * rows,
    })
    params = {"date_from": "20260401", "date_to": "20260930",
              "stock_cd_list": ["00001", "00247", "00901"],
              "product_group_list": ["A", "B"],
              "product_di_list": ["1", "2"],
              "product_class_list": ["1", "2"]}
    context = _build_sims_analysis_context_from_df(
        frame, result={}, action_name="품목별 매출 추세 요약표",
        params=params, meta={"display_row_count": 300})
    context["table_key"] = "synthetic-sales-trend"
    delivered = _model_payload(context, "품목별 매출 추세와 매출 합계를 분석해줘")
    assert "20260401" in delivered["query_scope_summary"]
    assert "재고위치" in delivered["query_scope_summary"]
    assert "제품그룹" in delivered["query_scope_summary"]
    facts = delivered["whole_table_facts"]
    assert facts["group_column"] == "추세판정"
    table = build_verified_group_table(facts, scope=delivered["query_scope_summary"],
                                       display_rows=delivered["display_row_count"])
    assert "총매출액" in table and "총출고수량" in table
    assert "전체 10,020건 기준 (화면 표시 300건)" in table
    total = int(frame["총매출액"].sum())
    sales_saved = saved_message(
        table, facts=facts,
        model_text=f"총매출액 합계는 {total:,}원입니다. 총매출액 합계는 9,999원입니다.")
    assert f"{total:,}원" in sales_saved["content"]
    assert "9,999원" not in sales_saved["content"]
    print("PASS sales trend scope, amount and display/source distinction")


if __name__ == "__main__":
    test_inbound_transport()
    test_generic_code_request()
    test_unverified_groups()
    test_sales_scope_and_amount()

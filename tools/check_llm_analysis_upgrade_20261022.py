"""Offline evidence and scope checks for the existing SIMS LLM-analysis button."""

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import (
    _build_sims_analysis_context_from_df,
    _build_sims_detail_analysis_prompt,
    _sims_clicked_llm_context_mismatch,
)


def context(frame, action, display_rows):
    meta = {"display_row_count": display_rows, "analysis_row_count": len(frame)}
    return _build_sims_analysis_context_from_df(
        frame, result={"meta": meta}, action_name=action, params={}, meta=meta
    )


def run():
    stock = pd.DataFrame({
        "제품코드": [f"{i:05d}" for i in range(620)],
        "제품명": [f"제품{i}" for i in range(620)],
        "재고상태": ["정상"] * 500 + ["부족"] * 120,
        "현재재고수량": [2] * 500 + [-1] * 120,
        "재고금액": [100] * 620,
        "Rd06_Password": ["private-secret"] * 620,
    })
    stock_before = stock.copy(deep=True)
    ctx = context(stock, "현재고 조회", 300)
    facts = ctx["whole_table_facts"]
    assert ctx["row_count"] == facts["row_count"] == 620
    assert ctx["display_row_count"] == 300
    assert facts["numeric_metrics"]["현재재고수량"]["sum"] == 880
    assert facts["numeric_metrics"]["재고금액"]["sum"] == 62000
    assert facts["group_rows_total"] == 620
    assert "private-secret" not in json.dumps(ctx, ensure_ascii=False, default=str)
    pd.testing.assert_frame_equal(stock, stock_before)
    ctx["table_key"] = "stock-table"
    assert not _sims_clicked_llm_context_mismatch(ctx, "stock-table", "현재고 조회")
    assert _sims_clicked_llm_context_mismatch(ctx, "other-table", "현재고 조회") == "table_key_mismatch"
    assert _sims_clicked_llm_context_mismatch(ctx, "stock-table", "발주조회") == "action_mismatch"
    print("PASS stock: full 620, display 300, sensitive data and table scope")

    product = pd.DataFrame({
        "제품코드": [f"{i:05d}" for i in range(453)],
        "제품명": [f"제품{i}" for i in range(453)],
        "출고빈도등급": ["A"] * 100 + ["F"] * 353,
        "추정기여금액": [10] * 453,
    })
    product_ctx = context(product, "제품정보 조회", 300)
    assert product_ctx["row_count"] == product_ctx["whole_table_facts"]["row_count"] == 453
    assert product_ctx["whole_table_facts"]["numeric_metrics"]["추정기여금액"]["sum"] == 4530
    assert not product_ctx["sales_time_profile"] and not product_ctx["sales_group_profile"]
    print("PASS product information: full 453, display 300, no invented sales profile")

    for action, frame, metric in (
        ("발주조회", pd.DataFrame({"발주거래처명": ["A", "B"], "발주수량": [3, 4], "미입고수량": [1, 2]}), "발주수량"),
        ("매출현황 조회", pd.DataFrame({"매출처명": ["A", "B"], "매출금액": [30, 40]}), "매출금액"),
        ("제품재고장 조회", pd.DataFrame({"제품명": ["A", "B"], "입고수량": [3, 4], "출고수량": [1, 2]}), "입고수량"),
        ("현재표 후속조회", pd.DataFrame({"제품명": ["A", "B"], "수량": [3, 4]}), "수량"),
    ):
        built = context(frame, action, len(frame))
        assert metric in built["whole_table_facts"]["numeric_metrics"], action
        assert built["whole_table_facts"]["numeric_metrics"][metric]["valid_count"] == 2
    assert context(pd.DataFrame(), "현재고 조회", 0) == {}
    sparse = context(pd.DataFrame({"제품명": ["A"]}), "제품정보 조회", 1)
    assert not sparse["whole_table_facts"]["numeric_metrics"]
    prompt = _build_sims_detail_analysis_prompt(
        action_name="현재표 후속조회", display_rows=300, download_rows=620,
        expected_rows=620, columns=["제품명", "수량"]
    )
    assert "1,200" not in prompt and "간단히 분석" not in prompt
    print("PASS order/sales/ledger/current table, sparse/empty, adaptive prompt")


if __name__ == "__main__":
    run()

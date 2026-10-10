"""DB-free Current Table source, dimension, facts, and button-scope regression."""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui import chat_middleware
from app.ui.current_table_followups.action_dispatcher import (
    build_current_table_interpretive_facts,
    select_current_table_analysis_context,
)
from app.ui.current_table_followups.generic import (
    _build_common_group_summary,
    _find_common_group_column,
)
from tools.check_nlq_regression_harness_alignment_20261006 import _main_namespace


def _facts(df: pd.DataFrame, query: str, action: str) -> dict:
    return build_current_table_interpretive_facts(df=df, query=query, source_action=action)


def main() -> int:
    rows = 9418
    products = pd.DataFrame({
        "제품코드": [f"{index:05d}" for index in range(rows)],
        "제품명": [f"제품 {index}" for index in range(rows)],
        "구분명": ["보험", "비보험"] * (rows // 2),
        "제약사": ["한미", "동제"] * (rows // 2),
        "제품그룹명": ["처방", "일반"] * (rows // 2),
        "제품분류명": ["의약품", "기타"] * (rows // 2),
        "출고빈도등급": ["A", "자료부족"] * (rows // 2),
        "3개월출고수량": [3, 4] * (rows // 2),
        "추정기여금액": [100, 200] * (rows // 2),
    })
    product_queries = (
        ("현재표 구분별 분석", "구분명"),
        ("현재표 구분명 분석", "구분명"),
        ("현재표 제품구분별 분석", "구분명"),
        ("현재표 제약사별 분석", "제약사"),
        ("현재표 제품그룹별 분석", "제품그룹명"),
        ("현재표 출고빈도등급별 분석", "출고빈도등급"),
    )
    for action in ("제품정보 조회", "제품코드조회", "제품마스터 조회"):
        for query, column in product_queries:
            result = _facts(products, query, action)
            assert result["status"] == "success", (action, query, result)
            assert result["input_row_count"] == rows, (action, query, result)
            whole = result["whole_table_facts"]
            assert whole["group_column"] == column, (action, query, whole)
            assert result["source_row_count"] == (rows // 2 if column == "출고빈도등급" else rows), query
            assert "제품코드" not in whole.get("numeric_metrics", {}), (action, query, whole)
            assert len(whole.get("representative_records", [])) <= 12, (action, query)
            if column == "구분명":
                assert _find_common_group_column(products, "현재표 구분별 집계") == column
                aggregate = _build_common_group_summary(products, column, source_action=action)
                counts = {item["group"]: item["rows"] for item in whole["group_distribution"]}
                assert counts == dict(zip(aggregate[column], aggregate["제품수"])), counts
                assert whole["numeric_metrics"]["3개월출고수량"]["sum"] == aggregate["3개월출고수량"].sum()

    unsupported = _facts(products, "현재표 거래처별 분석", "제품정보 조회")
    assert unsupported["status"] == "column_unavailable" and "거래처" in unsupported["capability"]["missing_columns"]
    trans_doc = pd.DataFrame({"거래명세서구분": ["매출", "매입"], "구분명": ["A", "B"], "제품코드": ["1", "2"], "거래금액": [10, 20]})
    wrong = _facts(trans_doc, "현재표 구분별 분석", "거래명세서 공통 조회")
    assert wrong["status"] == "column_unavailable" and "제품구분" not in wrong["capability"].get("requested_groupings", []), wrong

    other_sources = (
        ("현재고 조회", pd.DataFrame({"제품코드": ["1", "2"], "제품명": ["A", "B"], "재고수량": [3, 4]}), "현재표 제품별 분석", "제품명"),
        ("제품재고현황 조회", pd.DataFrame({"제품코드": ["1", "2"], "제품명": ["A", "B"], "재고수량": [3, 4]}), "현재표 제품별 분석", "제품명"),
        ("품목별 매출 예상", pd.DataFrame({"제품코드": ["1", "2"], "제품명": ["A", "B"], "제조사명": ["한미", "동제"], "총매출액": [10, 20]}), "현재표 제약사별 분석", "제조사명"),
        ("거래명세서 공통 조회", pd.DataFrame({"거래처명": ["A", "B"], "거래금액": [10, 20]}), "현재표 거래처별 분석", "거래처명"),
        ("발주계산", pd.DataFrame({"발주처명": ["A", "B"], "계산 발주수량": [1, 2]}), "현재표 발주처별 분석", "발주처명"),
    )
    for action, frame, query, column in other_sources:
        result = _facts(frame, query, action)
        assert result["status"] == "success", (action, result)
        if "whole_table_facts" in result:
            assert result["whole_table_facts"]["group_column"] == column, (action, result)
        else:
            assert result["group_column"] == column and result["metric_total"] == 30, (action, result)
    assert _facts(other_sources[0][1], "현재표 제약사별 분석", "현재고 조회")["status"] == "column_unavailable"

    source = products
    derived = _build_common_group_summary(source, "구분명", source_action="제품정보 조회")
    assert len(derived) == 2
    source_ctx = {"kind": "SIMS_ANALYSIS_CONTEXT_V1", "table_key": "original", "action": "제품정보 조회"}
    derived_ctx = {"kind": "SIMS_ANALYSIS_CONTEXT_V1", "table_key": "derived", "source_table_key": "original", "action": "현재표 구분명별 집계", "current_table_followup": True}
    state = {
        "__sims_current_table_source_key": "original",
        "__sims_current_table_source_action": "제품정보 조회",
        "__sims_last_table_key": "derived",
        "__sims_export_tables_by_key": {"original": source, "derived": derived},
        "sims_tables": {"original": source.head(300), "derived": derived},
        "__sims_analysis_ctx_by_table_key": {"original": source_ctx, "derived": derived_ctx},
    }
    namespace = _main_namespace(state, lambda *_args, **_kwargs: None)
    get_current = namespace["_current_table_get_latest_df"]
    for query in ("현재표 구분별 분석", "현재표 제품그룹별 분석", "현재표 구분명 분석"):
        selected, key = get_current()
        assert key == "original" and len(selected) == rows
        assert _facts(selected, query, "제품정보 조회")["input_row_count"] == rows
    assert select_current_table_analysis_context(state)[0]["table_key"] == "original"

    original_st = chat_middleware.st
    chat_middleware.st = SimpleNamespace(session_state=state)
    try:
        clicked, source_kind = chat_middleware._select_sims_analysis_ctx_for_table(table_key="derived", action="현재표 구분명별 집계")
        assert source_kind == "cache" and clicked["table_key"] == "derived"
        clicked_source, source_kind = chat_middleware._select_sims_analysis_ctx_for_table(table_key="original", action="제품정보 조회")
        assert source_kind == "cache" and clicked_source["table_key"] == "original"
    finally:
        chat_middleware.st = original_st

    state["__sims_export_tables_by_key"].pop("original")
    state["sims_tables"].pop("original")
    selected, key = get_current()
    assert selected is None and not key, "missing source must not fall back to a derived table"
    state["__sims_export_tables_by_key"]["new_original"] = other_sources[0][1]
    state["__sims_current_table_source_key"] = "new_original"
    selected, key = get_current()
    assert key == "new_original" and len(selected) == 2
    print("PASS: product/category aliases, seven source types, full-source facts, button scope, and fail-closed stash")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

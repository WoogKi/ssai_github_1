"""Full dispatcher and product-statistics LLM context regression, offline."""

import logging
from pathlib import Path
import sys
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ui.current_table_followups.action_dispatcher import (
    handle_current_table_followup_by_action, classify_current_table_followup_intent,
    build_current_table_interpretive_facts,
)
from app.ui.chat_middleware import _build_sims_analysis_context_from_df, _build_sims_detail_analysis_prompt
from app.ui.sims_analysis_profiles import build_sims_analysis_profile


def test_dispatcher():
    frame = pd.DataFrame({"제품코드": ["00001", "00002", "00003"], "제품명": ["same", "same", "other"],
                          "품목손익등급": ["A", "A", "D"], "품목기여등급": ["B", "B", "A"],
                          "출고빈도등급": ["E", "F", "X"], "추정기여금액": [10, 20, 100]})
    for query in ("현재표 손익등급별 분석", "현재표 품목손익등급 분석",
                  "현재표 기여도등급별 분석", "현재표 품목기여등급별 분석",
                  "현재표 출고빈도등급별 분석", "현재표 손익등급별 집계"):
        pushed, notices = [], []
        def push(**kwargs):
            pushed.append(kwargs)
            return True
        def notice(**kwargs):
            notices.append(kwargs)
            return True
        handled = handle_current_table_followup_by_action(
            df=frame, query=query, top_n=300, table_key="fixture", source_action="제품정보 조회",
            helpers={"push_table": push, "push_notice": notice, "find_col": lambda df, names: next((n for n in names if n in df), ""),
                     "to_num": lambda values: pd.to_numeric(values, errors="coerce"),
                     "add_seq": lambda df: df, "fmt_num": str}, log=logging.getLogger("fixture"), source_meta={},
        )
        expected_intent = "dataframe_table" if "집계" in query else "llm_analysis"
        assert classify_current_table_followup_intent(query) == expected_intent
        assert handled and len(pushed) == 1 and not notices, (query, pushed, notices)
        assert pushed[0]["df"]["제품수"].sum() == 3


def test_analysis_context():
    frame = pd.DataFrame({"제품코드": ["00001"], "제품명": ["fixture"], "품목손익등급": ["A"], "추정기여금액": [6780818], "3개월출고수량": [10]})
    meta = {"llm_summary_md": "전체 제품 1개, 품목손익등급 A 1개. 추정기여금액은 실제 매출액이 아님."}
    profile = build_sims_analysis_profile("제품정보 조회", columns=list(frame.columns))
    assert profile["profile_id"] == "snapshot_product_information"
    ctx = _build_sims_analysis_context_from_df(frame, result={"meta": meta}, action_name="제품정보 조회", params={}, meta=meta)
    assert not ctx["sales_time_profile"] and not ctx["sales_group_profile"]
    assert "실제 매출" in ctx["analysis_text"] and "전체 제품 1개" in ctx["analysis_text"]
    prompt = _build_sims_detail_analysis_prompt(action_name="제품정보 조회", display_rows=1, download_rows=1, expected_rows=1, columns=frame.columns)
    assert "추정기여금액" in prompt and "실제 매출액" in prompt and "일반화" in prompt
    for text in (prompt, ctx["analysis_text"]):
        assert "E는 낮은 출고빈도" in text and "M0~M2" in text and "최대 기여 등급" in text
    grouped = pd.DataFrame({"품목손익등급": ["A", "D"], "제품수": [2, 1], "추정기여금액": [30, 100]})
    action = "현재표 품목손익등급별 집계"
    group_ctx = _build_sims_analysis_context_from_df(grouped, result={"meta": {}}, action_name=action, params={}, meta={})
    group_prompt = _build_sims_detail_analysis_prompt(action_name=action, display_rows=2, download_rows=2, expected_rows=2, columns=grouped.columns)
    assert not group_ctx["sales_group_profile"] and not group_ctx["sales_time_profile"]
    for text in (group_ctx["analysis_text"], group_prompt):
        assert "그룹 컬럼은 분류 기준" in text and "품목기여등급으로 바꾸어" in text
    assert grouped["추정기여금액"].tolist() == [30, 100]


if __name__ == "__main__":
    test_dispatcher()
    print("PASS aggregation versus original-table LLM handoff (five queries)")
    test_analysis_context()
    print("PASS product-statistics analysis context/prompt (no database or LLM request)")

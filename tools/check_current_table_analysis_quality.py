"""No-DB tests of full-source facts, security, budget and table boundaries."""
from pathlib import Path
import sys
import json
import ast
import logging
import re
import uuid
from types import SimpleNamespace
from typing import Any, Optional
from dataclasses import replace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from app.ui.current_table_followups.action_dispatcher import (
    build_current_table_interpretive_facts, classify_current_table_followup_intent,
    handle_current_table_followup_by_action, is_explicit_current_table_followup_reference,
    select_current_table_analysis_context,
)
from app.ui.current_table_followups.analysis_facts import (
    fit_analysis_context,
    sanitize_current_table_analysis_output,
)
from app.ui.sims_analysis_profiles import build_response_format_instruction
from tools.check_nlq_casebook_current_table16 import _cases, _dispatch, _trend_frame


def main():
    checks = 0
    def check(condition, label):
        nonlocal checks
        assert condition, label
        checks += 1
        print("PASS", label)
    frame = pd.DataFrame({"제품명": ["제품A", "제품B", "제품C", "제품D", "합계"],
                          "제조사": ["제조A", "제조A", "제조B", "제조B", ""],
                          "재고수량": [100, 0, -2, None, 98], "재고금액": [1000, 0, -20, None, 980],
                          "입고단가": [10, 20, 30, 40, 100], "Rd06_Password": ["SECRET"] * 5})
    original = frame.copy(deep=True)
    for query in ("현재표 제조사명별 분석", "현재표 제조사명별 의미가 뭐야", "현재표 요약해줘"):
        facts = build_current_table_interpretive_facts(df=frame, query=query, source_action="제품재고현황 조회")
        whole = facts["whole_table_facts"]
        check(facts["source_row_count"] == 4 and facts["excluded_summary_row_count"] == 1, query + " detail scope")
        stats = whole["numeric_metrics"]["재고수량"]
        check(stats["sum"] == 98 and stats["zero_count"] == 1 and stats["negative_count"] == 1
              and stats["missing_or_invalid_count"] == 1, query + " actual statistics")
        check("sum" not in whole["numeric_metrics"]["입고단가"], "unit price is nonadditive")
        check("SECRET" not in json.dumps(whole, ensure_ascii=False) and "Rd06_Password" not in str(whole), "sensitive columns excluded")
        check(whole["table_records_complete"] and len(whole["table_records"]) == 4, "small safe whole table")
        if whole["group_column"]:
            check(stats["top_groups"][0]["group"] == "제조A" and stats["bottom_groups"][0]["value"] == -2,
                  "manufacturer differences and negative group")
    check(frame.equals(original), "input not mutated")
    variants = ("분석해줘", "자세히 분석해줘", "좀 분석해줘", "을 분석해줘",
                "을 자세히 분석해줘", "에 대해 분석해줘", "을 점검해줘",
                "을 자세히 요약해줘", "의미가 뭐야")
    for variant in variants:
        question = "현재표 예상등급 " + variant
        missing = build_current_table_interpretive_facts(df=frame, query=question, source_action="제품재고현황 조회")
        check(classify_current_table_followup_intent(question) == "llm_analysis"
              and missing["status"] == "column_unavailable"
              and missing["capability"]["requested_grouping"] == "forecast_grade"
              and "예상등급" in missing["capability"]["missing_columns"]
              and "whole_table_facts" not in missing, "explicit missing grade: " + variant)
    for question, label in (("현재표 추세판정 자세히 분석해줘", "추세판정"),
                            ("현재표 제조사명과 임의없는차원 자세히 분석해줘", "임의없는차원"),
                            ("현재표 임의없는차원 자세히 분석해줘", "임의없는차원")):
        missing = build_current_table_interpretive_facts(df=frame, query=question, source_action="제품재고현황 조회")
        check(missing["status"] == "column_unavailable" and label in missing["capability"]["missing_columns"]
              and "whole_table_facts" not in missing, "explicit unknown protected: " + question)
    manufacturer = build_current_table_interpretive_facts(df=frame, query="현재표 제조사명별 자세히 분석해줘",
                                                         source_action="제품재고현황 조회")
    check(manufacturer["status"] == "success" and manufacturer["capability"]["requested_grouping"] == "manufacturer"
          and manufacturer["whole_table_facts"]["group_column"] == "제조사", "modified existing manufacturer preserved")
    forecast = pd.DataFrame({"제품명": ["제품A", "제품B", "제품C"], "예상등급": ["안정", "감소예상", "감소예상"],
                             "다음월예상매출": [100, 20, 30]})
    whole = build_current_table_interpretive_facts(df=forecast, query="현재표 예상등급 분석해줘",
                                                  source_action="품목별 매출 예상")["whole_table_facts"]
    check(whole["numeric_metrics"]["다음월예상매출"]["sum"] == 150, "forecast without explicit metric")
    for test_frame, question, action, grouping in (
        (forecast, "현재표 예상등급 자세히 분석해줘", "품목별 매출 예상", "forecast_grade"),
        (_trend_frame(), "현재표 추세판정 자세히 분석해줘", "품목별 매출 추세 요약표", "trend_judgement"),
    ):
        selected = build_current_table_interpretive_facts(df=test_frame, query=question, source_action=action)
        check(classify_current_table_followup_intent(question) == "llm_analysis"
              and selected["status"] == "success" and selected["capability"]["requested_grouping"] == grouping,
              "modified existing dimension preserved: " + grouping)
    filter_case = next(c for c in _cases() if c.case_id == "NLQ-0022")
    question = "현재표 제조사명 한미약품 상세히 보여줘"
    filtered = _dispatch(replace(filter_case, query=question, runtime_query=question))
    check(classify_current_table_followup_intent(question) == "dataframe_table"
          and len(filtered[2]["df"]) == 2, "labelled manufacturer value filter unchanged")
    check(whole["group_distribution"][0]["rows"] == 2, "grade distribution")
    check(whole["numeric_metrics"]["다음월예상매출"]["top_group_share_pct"] == 100 / 150 * 100, "concentration")
    unknown = build_current_table_interpretive_facts(df=frame, query="현재표 제조사명별 임의없는차원별 분석",
                                                     source_action="제품재고현황 조회")
    check(unknown["status"] == "column_unavailable", "unknown dimension fail closed")
    large = pd.concat([forecast] * 1000, ignore_index=True)
    facts = build_current_table_interpretive_facts(df=large, query="현재표 예상등급 분석해줘", source_action="품목별 매출 예상")
    check(facts["whole_table_facts"]["numeric_metrics"]["다음월예상매출"]["sum"] == 150000, "all 3000 rows scanned")
    check(not facts["whole_table_facts"]["table_records_complete"], "large table not copied to prompt")
    wide = frame.copy()
    for i in range(12):
        wide.insert(0, f"기간{i}단가", 50)
    wide_facts = build_current_table_interpretive_facts(df=wide, query="현재표 제조사명별 분석",
                                                       source_action="제품재고현황 조회")["whole_table_facts"]
    check("재고수량" in wide_facts["selected_metrics"] and "재고금액" in wide_facts["selected_metrics"],
          "screen core metrics precede wide price columns")
    limited = build_current_table_interpretive_facts(df=large, query="현재표 예상등급 분석해줘", source_action="품목별 매출 예상",
                                                     source_meta={"limit_hit": True, "applied_download_limit_rows": 3000, "expected_rows": 5000})
    check(limited["analysis_scope"] == "limited_source", "existing source limit disclosed")
    context = fit_analysis_context({"whole_table_facts": facts["whole_table_facts"]}, 9000)
    check(len(json.dumps(context, ensure_ascii=False)) <= 9000, "valid bounded JSON")
    case = next(c for c in _cases() if c.case_id == "NLQ-0022")
    first = _dispatch(replace(case, query="현재표 제조사명별 요약표", runtime_query="현재표 제조사명별 요약표"))[2]["df"]
    second = _dispatch(replace(case, query="현재표 제조사별 요약표", runtime_query="현재표 제조사별 요약표"))[2]["df"]
    check(first.equals(second), "manufacturer name summary equals manufacturer summary")
    frequency = pd.DataFrame({"제품명": ["제품A", "제품B", "제품C"],
                              "출고빈도등급": ["A", "B", "A"], "재고수량": [10, 20, 30]})
    frequency_case = replace(case, frame=frequency,
                             query="현재표 출고빈도별 집계", runtime_query="현재표 출고빈도별 집계")
    alias_table = _dispatch(frequency_case)[2]["df"]
    canonical_table = _dispatch(replace(frequency_case, query="현재표 출고빈도등급별 집계",
                                        runtime_query="현재표 출고빈도등급별 집계"))[2]["df"]
    check(classify_current_table_followup_intent("현재표 출고빈도별 집계") == "dataframe_table"
          and alias_table.equals(canonical_table), "frequency alias equals canonical grade aggregation")
    grade_frame = pd.DataFrame({
        "제품명": ["제품A", "제품B", "제품C"],
        "출고빈도등급": ["A", "B", "A"],
        "품목기여등급": ["A", "X", "A"],
        "품목손익등급": ["B", "X", "B"],
        "재고수량": [10, 20, 30],
    })
    grade_case = replace(case, frame=grade_frame)
    for short_query, canonical_query, column in (
        ("현재표 출고빈도 집계", "현재표 출고빈도등급 집계", "출고빈도등급"),
        ("현재표 품목기여 집계", "현재표 품목기여등급 집계", "품목기여등급"),
        ("현재표 품목손익 집계", "현재표 품목손익등급 집계", "품목손익등급"),
    ):
        short_table = _dispatch(replace(grade_case, query=short_query, runtime_query=short_query))[2]["df"]
        canonical_grade_table = _dispatch(
            replace(grade_case, query=canonical_query, runtime_query=canonical_query)
        )[2]["df"]
        check(
            classify_current_table_followup_intent(short_query) == "dataframe_table"
            and short_table.equals(canonical_grade_table)
            and column in short_table.columns,
            f"{short_query} equals canonical grade aggregation",
        )
    for query, expected_intent in (
        ("현재표 출고빈도 분석", "llm_analysis"),
        ("현재표 품목기여 분석", "llm_analysis"),
        ("현재표 품목손익 분석", "llm_analysis"),
        ("현재표 출고빈도 집계", "dataframe_table"),
        ("현재표 품목기여 집계", "dataframe_table"),
        ("현재표 품목손익 집계", "dataframe_table"),
    ):
        check(
            is_explicit_current_table_followup_reference(query)
            and classify_current_table_followup_intent(query) == expected_intent,
            f"first-input current-table route: {query}",
        )
    chat_main_source = (Path(__file__).resolve().parents[1] / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    check(
        "explicit_current_table_reference = is_explicit_current_table_followup_reference(user_input)" in chat_main_source
        and "or explicit_current_table_reference" in chat_main_source,
        "first-input current-table reference enters SIMS follow-up route",
    )
    forced_followup_start = chat_main_source.index("elif is_current_table_forced_followup:")
    forced_followup_end = chat_main_source.index("elif (\n            is_implicit_analytics_current_followup", forced_followup_start)
    forced_followup_route = chat_main_source[forced_followup_start:forced_followup_end]
    check(
        "_push_current_table_followup_help(current_table_followup_input)" in forced_followup_route
        and "stage=immediate_clarification" in forced_followup_route,
        "unhandled explicit current-table follow-up terminates with clarification",
    )
    pushed_tables, pushed_notices = [], []

    def _current_table_find_col(frame, *, exact=(), include_any=(), exclude_any=()):
        columns = [str(column) for column in frame.columns]
        for column in exact:
            if column in columns:
                return column
        for column in columns:
            if include_any and not any(token in column for token in include_any):
                continue
            if exclude_any and any(token in column for token in exclude_any):
                continue
            return column
        return ""

    current_table_helpers = {
        "find_col": _current_table_find_col,
        "to_num": lambda series: pd.to_numeric(series, errors="coerce").fillna(0),
        "push_table": lambda **kwargs: pushed_tables.append(kwargs) or True,
        "push_notice": lambda **kwargs: pushed_notices.append(kwargs) or True,
    }
    current_stock_frequency = pd.DataFrame({
        "제품코드": ["P1", "P2", "P3"],
        "제품명": ["제품A", "제품B", "제품C"],
        "출고빈도등급": ["A", "F", "X"],
        "재고수량": [10, 20, 30],
    })
    check(
        handle_current_table_followup_by_action(
            df=current_stock_frequency,
            query="현재표 출고빈도별 집계",
            top_n=20,
            table_key="current_stock_frequency",
            source_action="현재고 조회",
            helpers=current_table_helpers,
            log=logging.getLogger(__name__),
        )
        and len(pushed_tables) == 1
        and not pushed_notices
        and set(pushed_tables[0]["df"]["출고빈도등급"]) == {"A", "F", "X"}
        and pushed_tables[0]["extra_meta"]["source_call_count"] == 0,
        "current stock frequency grouping uses attached grades without source calls",
    )
    pushed_tables.clear()
    pushed_notices.clear()
    current_stock_frequency_missing = current_stock_frequency.assign(출고빈도등급="빈도자료 부족")
    check(
        handle_current_table_followup_by_action(
            df=current_stock_frequency_missing,
            query="현재표 출고빈도별 집계",
            top_n=20,
            table_key="current_stock_frequency_missing",
            source_action="현재고 조회",
            helpers=current_table_helpers,
            log=logging.getLogger(__name__),
        )
        and not pushed_tables
        and len(pushed_notices) == 1
        and pushed_notices[0]["extra_meta"]["result_status"] == "no_data"
        and pushed_notices[0]["extra_meta"]["source_call_count"] == 0,
        "current stock unavailable frequency grades return notice instead of empty table",
    )
    hidden_keys = sanitize_current_table_analysis_output(
        "top5_group_share_pct은 16.05%이고 unknown_helper_key도 확인했습니다.", group_label="제조사"
    )
    check("top5_group_share_pct" not in hidden_keys and "unknown_helper_key" not in hidden_keys
          and "상위 5개 제조사 점유율은 16.05%" in hidden_keys,
          "final answer hides implementation keys with business labels")
    rule = build_response_format_instruction("현재표 자세히 분석해줘", current_table_analysis=True)
    check("8~12" not in rule and "3~5" not in rule and "고정 줄수" in rule, "adaptive current table response")
    check("8~12" in build_response_format_instruction("분석해줘"), "other SIMS prompts unchanged")
    # Run actual source-selection/preparation functions without starting Streamlit or querying ERP.
    source = (Path(__file__).resolve().parents[1] / "app/Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    names = {
        "_current_table_get_latest_df",
        "_prepare_current_table_analysis_override",
        "_push_no_current_table_notice",
        "_push_current_table_followup_help",
        "build_messages_with_system",
    }
    module = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[])
    state = {"__sims_current_table_source_key": "fixture", "__sims_last_table_key": "fixture",
             "__sims_current_table_source_action": "품목별 매출 예상",
             "__sims_export_tables_by_key": {"fixture": large}, "sims_tables": {"fixture": forecast.head(1)},
             "__sims_current_table_source_analysis_ctx": {"kind": "SIMS_ANALYSIS_CONTEXT_V1", "table_key": "fixture",
                                                          "action": "품목별 매출 예상"}}
    notices = []
    env = {"pd": pd, "Any": Any, "Optional": Optional, "json": json, "re": re, "uuid": uuid,
           "st": SimpleNamespace(session_state=state), "log": logging.getLogger(__name__),
           "select_current_table_analysis_context": select_current_table_analysis_context,
           "classify_current_table_followup_intent": classify_current_table_followup_intent,
           "build_current_table_interpretive_facts": build_current_table_interpretive_facts,
           "_current_table_push_notice": lambda **kwargs: notices.append(kwargs) or True,
           "is_sims_related_question": lambda q: True, "is_general_writing_request": lambda q: False,
           "BASE_SYSTEM_PROMPT": "분석 답변은 짧게 정리하라.", "GENERAL_SYSTEM_PROMPT": "일반 대화",
           "get_sims_context_data": lambda **kw: None, "get_sims_context_text": lambda **kw: None,
           "fit_analysis_context": fit_analysis_context,
           "_clip_for_model": lambda text, limit: text[:limit],
           "build_response_format_instruction": build_response_format_instruction}
    exec(compile(module, "current-table-production-functions", "exec"), env)
    for question in ("현재표", "현재표?", "현재 조회결과", "현재표 알수없는표현"):
        notices.clear()
        check(
            env["_push_current_table_followup_help"](question)
            and len(notices) == 1
            and notices[-1]["extra_meta"]["result_status"] == "clarification",
            "explicit current-table unclear request receives notice: " + question,
        )
    notices.clear()
    check(
        env["_push_no_current_table_notice"]("현재표")
        and len(notices) == 1,
        "no-current-table explicit request receives notice",
    )
    notices.clear()
    check(env["_prepare_current_table_analysis_override"]("현재표 예상등급 분석해줘"), "runtime context prepared")
    ctx = state["__current_table_analysis_ctx_override"]
    check(ctx["row_count"] == 3000 and ctx["whole_table_facts"]["numeric_metrics"]["다음월예상매출"]["sum"] == 150000,
          "runtime selects export full source over display row")
    check(not notices and ctx["analysis_target"] == "current_table_whole_facts", "runtime rich facts target")
    fitted = fit_analysis_context(ctx, 9000)
    check(len(json.dumps(fitted, ensure_ascii=False)) <= 9000, "runtime minimum context budget")
    messages = env["build_messages_with_system"]([{"role": "assistant", "content": "이전 표"}],
                                                   user_text="현재표 예상등급 분석해줘", analysis_ctx_override=ctx)
    check(len(messages) == 2 and messages[1]["role"] == "user", "runtime one request excludes old conversation")
    system = messages[0]["content"]
    check("고정 줄수, 수치 개수" in system and "짧게 정리/고정 섹션 지침보다" in system,
          "runtime adaptive instructions override base prompt")
    check("[CURRENT_TABLE_ANALYSIS_OUTPUT]" in system
          and "어떤 key도 그대로 쓰지 말고" in system,
          "runtime prompt prohibits implementation keys")
    delivered = json.loads(system.split("[SIMS_JSON]\n", 1)[1].split("\n[/SIMS_JSON]", 1)[0])
    check(delivered["whole_table_facts"]["numeric_metrics"]["다음월예상매출"]["sum"] == 150000,
          "runtime final model messages include whole-table statistics")
    check(not env["_prepare_current_table_analysis_override"]("현재표 임의없는차원별 분석")
          and notices[-1]["action"] == "현재표 컬럼 부족", "runtime unknown dimension notice")
    check("__current_table_analysis_ctx_override" not in state, "runtime removes stale override on blocked analysis")
    state["__sims_current_table_source_action"] = "제품재고현황 조회"
    state["__sims_current_table_source_analysis_ctx"]["action"] = "제품재고현황 조회"
    state["__sims_export_tables_by_key"]["fixture"] = frame
    for question in ("현재표 예상등급 자세히 분석해줘", "현재표 제조사명과 임의없는차원 자세히 분석해줘"):
        check(not env["_prepare_current_table_analysis_override"](question)
              and notices[-1]["action"] == "현재표 컬럼 부족"
              and "__current_table_analysis_ctx_override" not in state, "runtime missing dimension blocked: " + question)
    state["__sims_current_table_source_action"] = "현재고 조회"
    state["__sims_current_table_source_analysis_ctx"]["action"] = "현재고 조회"
    state["__sims_export_tables_by_key"]["fixture"] = grade_frame
    notices.clear()
    for question in ("현재표 출고빈도 분석", "현재표 품목기여 분석", "현재표 품목손익 분석"):
        check(
            env["_prepare_current_table_analysis_override"](question)
            and state["__current_table_analysis_query"] == question
            and not notices,
            "first-input grade analysis prepares current-table LLM handoff: " + question,
        )
    print(f"SUMMARY {checks}/{checks} PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

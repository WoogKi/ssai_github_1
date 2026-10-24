"""DB-free model-message and fragment checks for SIMS analysis context delivery."""

from __future__ import annotations

import argparse
import ast
import json
import logging
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from typing import Any, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import _build_sims_analysis_context_from_df
from app.ui.current_table_followups.analysis_facts import (
    build_structured_analysis_options, build_verified_group_table,
    build_verified_whole_table, fit_analysis_context, structured_analysis_code_id,
)
from app.ui.sims_analysis_profiles import build_response_format_instruction
from tools.check_llm_analysis_accuracy_20261022 import GRADE_TOTALS, stock_fixture


SOURCE = Path(__file__).resolve().parents[1] / "app/Lmstudio_SSAI_chat_main.py"


def runtime(context_chars: int):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
    names = {"build_messages_with_system", "_run_sims_llm_analysis_from_fragment"}
    functions = [node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == 2
    state = {"__model_ctx_len": context_chars}
    warnings = []
    calls = []
    env = {
        "Any": Any, "Optional": Optional, "json": json, "re": re,
        "st": SimpleNamespace(session_state=state, warning=warnings.append, error=warnings.append),
        "log": logging.getLogger("llm_context_gate"),
        "BASE_SYSTEM_PROMPT": "분석 답변은 ①핵심 요약 ②주요 수치 ③주의/확인할 점 ④다음 조회 제안 순서로 짧게 정리하라.",
        "GENERAL_SYSTEM_PROMPT": "일반 대화",
        "_SIMS_BRIEF_ANALYSIS_RULE": "분석 답변은 ①핵심 요약 ②주요 수치 ③주의/확인할 점 ④다음 조회 제안 순서로 짧게 정리하라.",
        "is_sims_related_question": lambda _: True,
        "is_general_writing_request": lambda _: False,
        "get_sims_context_data": lambda **_: None,
        "get_sims_context_text": lambda **_: None,
        "fit_analysis_context": fit_analysis_context,
        "build_verified_group_table": build_verified_group_table,
        "build_verified_whole_table": build_verified_whole_table,
        "build_structured_analysis_options": build_structured_analysis_options,
        "structured_analysis_code_id": structured_analysis_code_id,
        "_clip_for_model": lambda value, limit: str(value)[:limit],
        "build_response_format_instruction": build_response_format_instruction,
        "current_room": {"id": "fixture", "company": "4"},
        "stream_and_append_assistant": lambda **kw: calls.append(kw),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), env)
    return env, state, warnings, calls


def stock_context(frame: pd.DataFrame, location_map: dict[str, str], source_params: dict | None = None) -> dict:
    params = source_params or {
        "stock_cds": ["00901"], "stock_cd_list": ["00901"], "stock_cd": "00901",
        "stock_location_name_map": location_map, "stock_mode": "실재고",
    }
    context = _build_sims_analysis_context_from_df(
        frame, result={}, action_name="현재고 조회", params=params,
        meta={"display_row_count": 300, "analysis_row_count": len(frame)},
    )
    context["table_key"] = "stock-4-00901"
    return context


def assert_message(frame: pd.DataFrame, location_map: dict[str, str], model_ctx: int,
                   source_params: dict | None = None) -> int:
    context = stock_context(frame, location_map, source_params)
    assert "stock_location_name_map" not in context["params"]
    assert "00901" in context["query_scope_summary"]
    assert context["whole_table_facts"]["group_consistency_pass"] is True
    env, state, warnings, calls = runtime(model_ctx)
    env["_run_sims_llm_analysis_from_fragment"](
        "현재고 분석", analysis_ctx_override=context,
        clicked_table_key="stock-4-00901", clicked_action="현재고 조회",
    )
    assert not warnings and len(calls) == 1
    messages = calls[0]["messages_for_ai"]
    system = messages[0]["content"]
    raw = system.split("[SIMS_JSON]\n", 1)[1].split("\n[/SIMS_JSON]", 1)[0]
    delivered = json.loads(raw)
    limit = 9000 if model_ctx <= 12288 else 12000
    assert len(raw) <= limit and delivered["row_count"] == len(frame)
    assert delivered["display_row_count"] == 300
    assert delivered["whole_table_facts"]["group_consistency_pass"] is True
    assert delivered["whole_table_facts"]["group_count"] == 8
    assert delivered["sample_records"]
    assert "stock_location_name_map" not in raw
    assert state.get("__sims_analysis_attach_error") is None
    for metric in ("재고수량", "보험금액"):
        stats = delivered["whole_table_facts"]["numeric_metrics"][metric]
        grouped = stats["group_values"]
        assert stats["group_values_complete"] is True
        assert {row["group"] for row in grouped} == set(GRADE_TOTALS)
        assert sum(row["rows"] for row in grouped) == len(frame)
        assert sum(row["value"] for row in grouped) == stats["sum"]
        assert stats["sum"] == pd.to_numeric(frame[metric]).sum()
    amount = delivered["whole_table_facts"]["numeric_metrics"]["보험금액"]
    assert amount["zero_count"] == int(frame["보험금액"].eq(0).sum())
    assert amount["missing_or_invalid_count"] == int(frame["보험금액"].isna().sum())
    return len(raw)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-xlsx", type=Path)
    parser.add_argument("--source-log", type=Path)
    args = parser.parse_args()
    original = pd.read_excel(args.source_xlsx) if args.source_xlsx else stock_fixture()
    if not args.source_xlsx:
        for index in range(23 - len(original.columns)):
            original[f"부가{index}"] = ""
    assert len(original) == 600 and len(original.columns) == 23
    synthetic_621 = pd.concat([original, original.iloc[:21]], ignore_index=True)
    location_map = {f"{index:05d}": f"재고위치{index:05d}" for index in range(74)}
    location_map["00901"] = "오토스토어 대기창고"
    source_params = None
    if args.source_log:
        source_lines = args.source_log.read_text(encoding="utf-8").splitlines()
        source_line = next(line for line in source_lines
                           if "[nlq.router] io handled action='현재고 조회'" in line
                           and "'stock_cd': '00901'" in line)
        source_params = ast.literal_eval(source_line.split("params=", 1)[1])
        location_map = source_params["stock_location_name_map"]
        assert len(location_map) == 74

    size_600 = assert_message(original, location_map, 8192, source_params)
    size_621 = assert_message(synthetic_621, location_map, 8192, source_params)
    size_621_wide = assert_message(synthetic_621, location_map, 16384, source_params)
    print(f"PASS 600/621 end-to-end JSON: 9000 chars {size_600}/{size_621}, 12000 chars {size_621_wide}")

    context = stock_context(synthetic_621, location_map, source_params)
    env, state, warnings, calls = runtime(8192)
    context["whole_table_facts"] = {**context["whole_table_facts"], "group_consistency_pass": False}
    env["_run_sims_llm_analysis_from_fragment"](
        "현재고 분석", analysis_ctx_override=context,
        clicked_table_key="stock-4-00901", clicked_action="현재고 조회",
    )
    assert not calls and "__sims_analysis_attach_error" not in state
    assert "합계가 일치하지 않아" in warnings[-1]
    print("PASS mismatched group totals block model call")

    context = stock_context(synthetic_621, location_map, source_params)
    context["whole_table_facts"] = {**context["whole_table_facts"], "screen_purpose": "X" * 20000}
    env, state, warnings, calls = runtime(8192)
    env["_run_sims_llm_analysis_from_fragment"](
        "현재고 분석", analysis_ctx_override=context,
        clicked_table_key="stock-4-00901", clicked_action="현재고 조회",
    )
    assert not calls and "__sims_analysis_attach_error" not in state
    assert "전달 한도를 초과" in warnings[-1]
    print("PASS genuine capacity error stays fail-closed with specific notice")

    context = stock_context(synthetic_621, location_map, source_params)
    env, _, warnings, calls = runtime(8192)
    env["_run_sims_llm_analysis_from_fragment"](
        "현재고 분석", analysis_ctx_override=context,
        clicked_table_key="different-table", clicked_action="현재고 조회",
    )
    assert not calls and "일치하지 않아" in warnings[-1]
    print("PASS clicked-table context isolation")


if __name__ == "__main__":
    main()

"""DB-free check of the exact Excel aggregation and saved LLM analysis message."""

from __future__ import annotations

import ast
from copy import deepcopy
from contextlib import nullcontext
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import _build_sims_analysis_context_from_df
from app.ui.current_table_followups.analysis_facts import (
    build_verified_group_table,
    compose_structured_analysis_output,
    compose_verified_analysis_output,
    structured_analysis_code_id,
)
from tools.check_llm_analysis_context_fix_20261022 import runtime


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "app/Lmstudio_SSAI_chat_main.py"
EXCEL = Path(r"C:\New\Python_Project\_codex_diffs\LmStudion_project1\20261022\현재고_조회_20261022_133400.xlsx")


def saved_message(table: str, *, facts: dict | None = None,
                  model_text: str = "B등급의 재고금액 집중을 확인하세요.\n기타 22건 579개 2,850,884원입니다.",
                  structured_options: list[dict] | None = None,
                  stream_error: bool = False) -> dict:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "stream_and_append_assistant")
    shown = []
    class Slot:
        def markdown(self, value):
            shown.append(value)
        def caption(self, _value):
            pass
        def empty(self):
            pass
    class Streamlit:
        session_state = {"selected_model": "fixture"}
        def chat_message(self, _role):
            return nullcontext()
        def empty(self):
            return Slot()
        def markdown(self, value):
            shown.append(value)
        def caption(self, _value):
            pass
    chunk = SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=model_text,
                              reasoning_content=""), finish_reason="stop")])
    def chunks():
        yield chunk
        if stream_error:
            raise RuntimeError("fixture_disconnect")
    saves = []
    env = {
        "List": List, "Dict": Dict, "Any": Any, "Optional": Optional,
        "EXPECTED_LM_MODEL": "fixture", "st": Streamlit(), "log": logging.getLogger(__name__),
        "LLM_SAFE_MESSAGES": {"empty_response": "", "reasoning_only": "", "model_not_loaded": ""},
        "_llm_request_config_ready": lambda _: (True, ""),
        "call_chat_protected": lambda **_: chunks(),
        "classify_llm_exception": lambda exc: {
            "code": "fixture_disconnect", "status": None,
            "exception_type": type(exc).__name__},
        "bounded_retry_count": lambda _: 0,
        "compose_verified_analysis_output": compose_verified_analysis_output,
        "compose_structured_analysis_output": compose_structured_analysis_output,
        "structured_analysis_code_id": structured_analysis_code_id,
        "sanitize_current_table_analysis_output": lambda text, **_: text,
        "make_ts": lambda: "fixture-time", "_next_seq": lambda: 1,
        "_message_meta": lambda _: {}, "_sync_room_meta": lambda *_, **__: None,
        "save_chat_rooms": lambda: saves.append(True),
        "_chat_log_kv": lambda _: "fixture",
        "_render_assistant_message_controls": lambda *_, **__: None,
    }
    import re
    import uuid
    env.update(re=re, uuid=uuid)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), env)
    room = {"messages": []}
    result = env["stream_and_append_assistant"](
        [{"role": "system", "content": "fixture"}], room,
        temperature=0.2, max_retries=0, history_channel="sims_messages",
        verified_analysis_table=table,
        verified_analysis_facts=facts,
        structured_analysis_options=structured_options,
    )
    assert len(saves) == 1 and result == room["messages"][0]["content"]
    assert result == room["sims_messages"][0]["content"] == shown[-1]
    if facts is not None:
        assert all(not value.endswith("▌") for value in shown[:-1]), shown
        if model_text:
            assert all(model_text not in value for value in shown[:-1]), shown
    if table:
        assert any("해석 생성 중" in value for value in shown[:-1])
    assert "2,850,884" not in result
    return room["messages"][0]


def main() -> None:
    frame = pd.read_excel(EXCEL)
    assert frame.shape == (615, 23)
    context = _build_sims_analysis_context_from_df(
        frame, result={}, action_name="현재고 조회",
        params={"stock_cd": "00901"}, meta={"display_row_count": 300},
    )
    facts = context["whole_table_facts"]
    assert facts["row_count"] == 615 and facts["group_count"] == 8
    assert facts["group_consistency_pass"] is True
    metrics = facts["numeric_metrics"]
    assert metrics["재고수량"]["sum"] == 12456
    assert metrics["보험금액"]["sum"] == 460022038
    assert metrics["보험금액"]["zero_count"] == 7
    assert metrics["보험금액"]["missing_or_invalid_count"] == 0
    expected = {
        "A": (30, 2036, 56818592), "B": (75, 3316, 189770118),
        "C": (112, 2823, 81802792), "D": (143, 1859, 65452068),
        "E": (233, 1829, 62108376), "F": (5, 19, 1209590),
        "X": (9, 42, 608470), "빈도자료 부족": (8, 532, 2252032),
    }
    for grade, (rows, quantity, amount) in expected.items():
        assert next(item["rows"] for item in facts["group_distribution"] if item["group"] == grade) == rows
        assert next(item["value"] for item in metrics["재고수량"]["group_values"] if item["group"] == grade) == quantity
        assert next(item["value"] for item in metrics["보험금액"]["group_values"] if item["group"] == grade) == amount
    table = build_verified_group_table(facts, scope=context["query_scope_summary"], display_rows=300)
    assert "| F·X·자료부족 소계 | 22 | 3.58% | 593 | 4.76% | 4,070,092 | 0.88% |" in table
    assert "| 전체 | 615 | 100.00% | 12,456 | 100.00% | 460,022,038 | 100.00% |" in table
    assert "전체 615건 기준 (화면 표시 300건)" in table
    assert "37.89% | 1,829 | 14.68% | 62,108,376 | 13.50%" in table
    assert table.index("| A |") < table.index("| F |") < table.index("| X |")
    assert saved_message(table)["content"].startswith(table)

    missing = frame.copy()
    missing.loc[0, "보험금액"] = None
    missing_ctx = _build_sims_analysis_context_from_df(
        missing, result={}, action_name="현재고 조회", params={}, meta={"display_row_count": 300})
    missing_table = build_verified_group_table(missing_ctx["whole_table_facts"])
    assert "실제 0값 7건, 결측·비유효 1건" in missing_table
    inconsistent = dict(facts, group_consistency_pass=False)
    try:
        build_verified_group_table(inconsistent)
    except ValueError as exc:
        assert str(exc) == "group_totals_mismatch"
    else:
        raise AssertionError("Mismatched totals must block rendering")
    assert build_verified_group_table(dict(facts, group_column="")) == ""

    context["table_key"] = "source-4"
    facts_before = deepcopy(facts)
    env, _, warnings, calls = runtime(8192)
    env["_run_sims_llm_analysis_from_fragment"](
        "현재고 분석", analysis_ctx_override=context,
        clicked_table_key="source-4", clicked_action="현재고 조회",
    )
    assert not warnings and len(calls) == 1
    assert calls[0]["verified_analysis_table"] == table
    delivered_facts = calls[0]["verified_analysis_facts"]
    assert build_verified_group_table(delivered_facts, scope=context["query_scope_summary"], display_rows=300) == table
    assert delivered_facts["numeric_metrics"]["보험금액"]["sum"] == facts["numeric_metrics"]["보험금액"]["sum"]
    assert context["whole_table_facts"] == facts_before
    assert "그룹·지표를 명시" in calls[0]["messages_for_ai"][0]["content"]
    print("PASS Excel 615/23, eight grades, totals/shares, zero/null, saved output, model boundary")


if __name__ == "__main__":
    main()

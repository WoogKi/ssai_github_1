"""DB-free button-to-saved-answer regression for structured SIMS analysis."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import _build_sims_analysis_context_from_df
from app.ui.current_table_followups.analysis_facts import (
    build_structured_analysis_options, compose_structured_analysis_output,
    structured_analysis_code_id,
)
from tools.check_llm_analysis_context_fix_20261022 import runtime
from tools.check_llm_analysis_verified_output_20261022 import saved_message


ROOT = Path(r"C:\New\Python_Project\_codex_diffs\LmStudion_project1\20261024")
CASES = (
    ("입고예정조회", "입고예정조회_20261024_181913.xlsx", 3174, 300),
    ("입고명세 조회", "입고명세_조회_20261024_181836.xlsx", 929, 200),
    ("출고명세 조회", "출고명세_조회_20261024_181854.xlsx", 7113, 200),
)
FALSE_CLAIMS = (
    "상위 매입처: 1. 2. 3.", "관리 제안:", "순으로 나타납니다",
    "단가적용처명이 많이 비어", "반품이나 조정", "세금계산서 미처리",
)


def check_case(action: str, filename: str, rows: int, display: int) -> None:
    source = pd.read_excel(ROOT / filename, dtype=object)
    context = _build_sims_analysis_context_from_df(
        source, result={}, action_name=action,
        params={"date_from": "20261023", "date_to": "20261023"},
        meta={"display_row_count": display},
    )
    context["table_key"] = "structured-" + action
    assert len(source) == rows and context["whole_table_facts"]["row_count"] == rows
    env, _, warnings, calls = runtime(16384)
    env["build_structured_analysis_options"] = build_structured_analysis_options
    records = []
    class Logger:
        def info(self, message, *args):
            records.append(message % args)
        def debug(self, *_args):
            pass
        def warning(self, *_args):
            pass
        def exception(self, *_args):
            pass
    env["log"] = Logger()

    def stream(**kwargs):
        calls.append(kwargs)

    env["stream_and_append_assistant"] = stream
    env["_run_sims_llm_analysis_from_fragment"](
        action + " 분석", analysis_ctx_override=context,
        clicked_table_key=context["table_key"], clicked_action=action,
    )
    assert not warnings and len(calls) == 1
    assert any("[llm.analysis.path]" in entry and
               "path=structured_evidence" in entry and
               f"code_id={structured_analysis_code_id()}" in entry for entry in records)
    call = calls[0]
    assert "VERIFIED_EVIDENCE_OPTIONS" in call["messages_for_ai"][0]["content"]
    options = call["structured_analysis_options"]
    assert options and all(item["evidence_id"] and item["check_id"] for item in options)
    facts = call["verified_analysis_facts"]
    assert facts["row_count"] == rows
    assert facts["display_row_count"] == display
    assert facts["query_scope_summary"] == context["query_scope_summary"]
    table = call["verified_analysis_table"]
    valid = json.dumps({"items": [{"evidence_id": options[0]["evidence_id"],
                                   "check_id": options[0]["check_id"]}]}, ensure_ascii=False)
    bad = json.dumps({"items": [{"evidence_id": options[0]["evidence_id"],
                                 "check_id": options[0]["check_id"],
                                 "explanation": "상위 매입처: 1. 2. 3. 단가적용처명이 많이 비어 있습니다. "
                                                "반품이나 조정, 세금계산서 미처리 때문입니다."},
                                {"evidence_id": "invented", "check_id": "invented"}]}, ensure_ascii=False)
    for model_text in (valid, bad, '{"items": [{"evidence_id": "invented", "check_id": "x"}]}',
                       "", "not json", "1. 2. 3. 관리 제안: 순으로 나타납니다"):
        result = saved_message(table, facts=facts, model_text=model_text,
                               structured_options=options)["content"]
        expected = compose_structured_analysis_output(table, model_text, facts=facts, options=options)
        assert result == expected
        assert "확인된 특징과 확인사항" in result
        if not table:
            assert f"화면 표시 {display:,}건" in result
        assert "원전표" in result or "입고 상태" in result
        assert all(claim not in result for claim in FALSE_CLAIMS), result
        if table:
            assert result.startswith(table)
    interrupted = saved_message(table, facts=facts, model_text=bad,
                                structured_options=options, stream_error=True)["content"]
    assert all(claim not in interrupted for claim in FALSE_CLAIMS)
    assert "확인된 특징과 확인사항" in interrupted
    findings = facts["operational_findings"]
    fallback = compose_structured_analysis_output(table, "", facts=facts, options=options)
    for key in ("due_date_top", "vendor_top", "product_top"):
        for item in findings.get(key) or []:
            assert item["name"] in fallback
            assert f"{item['quantity']:,.0f}개" in fallback
    if "supply_difference_rows" in findings:
        assert f"{findings['supply_difference_rows']:,}건" in fallback
        assert f"{findings['supply_difference_sum']:+,.0f}원" in fallback
        assert f"{findings['negative_quantity_rows']:,}건" in fallback
    if action == "입고명세 조회":
        assert "Y 929건" in fallback and "세금계산서 비교값 없음" in fallback


def main() -> None:
    for case in CASES:
        check_case(*case)
    print("PASS structured analysis button, read-only Excel, verified screen/save")


if __name__ == "__main__":
    main()

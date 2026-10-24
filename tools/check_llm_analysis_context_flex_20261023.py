"""DB-free context budget regression at the model-message boundary."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import _build_sims_analysis_context_from_df
from app.ui.current_table_followups.analysis_facts import build_verified_group_table, fit_analysis_context
from tools.check_llm_analysis_context_fix_20261022 import runtime


EXCEL = Path(r"C:\New\Python_Project\_codex_diffs\LmStudion_project1\20261022\현재고_조회_20261022_133400.xlsx")


def chars(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def context(frame: pd.DataFrame, *, action: str, scope: dict, table_key: str) -> dict:
    result = _build_sims_analysis_context_from_df(
        frame, result={}, action_name=action, params=scope,
        meta={"display_row_count": min(len(frame), 300), "analysis_row_count": len(frame)},
    )
    result["table_key"] = table_key
    return result


def delivered(context_value: dict, model_ctx: int) -> tuple[dict, int]:
    source = json.dumps(context_value, ensure_ascii=False, default=str)
    env, state, warnings, calls = runtime(model_ctx)
    env["_run_sims_llm_analysis_from_fragment"](
        "현재 조회 결과를 분석해줘", analysis_ctx_override=context_value,
        clicked_table_key=context_value["table_key"], clicked_action=context_value["action"],
    )
    assert not warnings and len(calls) == 1, (warnings, state.get("__sims_analysis_attach_error"))
    system = calls[0]["messages_for_ai"][0]["content"]
    payload = system.split("[SIMS_JSON]\n", 1)[1].split("\n[/SIMS_JSON]", 1)[0]
    result = json.loads(payload)
    limit = 9000 if model_ctx <= 12288 else 12000 if model_ctx <= 16384 else 18000
    assert len(payload) <= limit
    assert source == json.dumps(context_value, ensure_ascii=False, default=str)
    assert result["table_key"] == context_value["table_key"]
    assert result["row_count"] == context_value["row_count"]
    assert result["whole_table_facts"]["group_consistency_pass"] is True
    assert build_verified_group_table(result["whole_table_facts"]) == build_verified_group_table(
        context_value["whole_table_facts"])
    assert "고정 목차" in system and "짧게 정리하라" not in system
    assert state.get("__sims_analysis_attach_error") is None
    return result, len(payload)


def main() -> None:
    stock = pd.read_excel(EXCEL)
    assert stock.shape == (615, 23)
    stock_context = context(stock, action="현재고 조회", scope={"stock_cd": "00901"}, table_key="stock-00901")
    for model_ctx in (8192, 16384, 32768):
        fitted, length = delivered(stock_context, model_ctx)
        metrics = fitted["whole_table_facts"]["numeric_metrics"]
        assert fitted["whole_table_facts"]["group_count"] == 8
        assert metrics["재고수량"]["sum"] == 12456
        assert metrics["보험금액"]["sum"] == 460022038
        print(f"PASS stock 615 context={model_ctx} chars={length}")
    stock_703 = pd.concat([stock, stock.iloc[:88]], ignore_index=True)
    fitted_703, length_703 = delivered(
        context(stock_703, action="현재고 조회", scope={"stock_cd": "00901"}, table_key="stock-703"),
        16384,
    )
    assert fitted_703["row_count"] == 703
    print(f"PASS synthetic stock 703 chars={length_703}")

    two = stock.head(2).copy()
    for index in range(52 - len(two.columns)):
        two[f"추가매출금액{index}"] = index + 1
    two_context = context(two, action="품목별 매출 예상", scope={"제품명": "예시"}, table_key="product-two")
    two_context["analysis_text"] += two_context["query_scope_summary"] * 1500
    assert chars(two_context) >= 16810
    two_delivered, two_size = delivered(two_context, 16384)
    assert len(two_delivered["sample_records"]) == 2
    assert two_delivered["column_count"] == 52
    print(f"PASS synthetic two rows 52 columns {chars(two_context)}->{two_size}, details=2")

    count = 82688
    wide = stock.iloc[np.arange(count) % len(stock)].reset_index(drop=True).copy()
    for index in range(56 - len(wide.columns)):
        wide[f"추가매출금액{index}"] = (np.arange(count) % 19) + index
    large_context = context(wide, action="품목별 매출 추세 분석", scope={"기간": "202607~202609"}, table_key="sales-large")
    assert large_context["row_count"] == count and large_context["column_count"] == 56
    large_delivered, large_size = delivered(large_context, 16384)
    assert large_delivered["whole_table_facts"]["group_rows_total"] == count
    assert large_delivered["whole_table_facts"]["numeric_metrics"]["보험금액"]["sum"] == large_context["whole_table_facts"]["numeric_metrics"]["보험금액"]["sum"]
    assert large_delivered["delivery_scope"]["source_rows"] == count
    assert large_delivered["delivery_scope"]["sample_records"] == [2, 30]
    assert large_delivered["delivery_scope"]["group_detail_fields_omitted"] is True
    assert "analysis_text" in large_delivered
    print(f"PASS synthetic 82688 rows 56 columns {chars(large_context)}->{large_size}")

    deterministic = {
        "kind": "SIMS_ANALYSIS_CONTEXT_V1",
        "analysis_target": "current_table_deterministic_facts",
        "table_key": "derived-two",
        "action": "현재표 집계",
        "row_count": 2,
        "current_table_interpretive_facts": {"rows": [{"group": "A", "value": 7}]},
    }
    env, _, warnings, calls = runtime(16384)
    env["_run_sims_llm_analysis_from_fragment"](
        "현재표 분석", analysis_ctx_override=deterministic,
        clicked_table_key="derived-two", clicked_action="현재표 집계",
    )
    assert not warnings and len(calls) == 1
    payload = calls[0]["messages_for_ai"][0]["content"].split("[SIMS_JSON]\n", 1)[1].split("\n[/SIMS_JSON]", 1)[0]
    assert json.loads(payload)["current_table_interpretive_facts"] == deterministic["current_table_interpretive_facts"]
    print("PASS deterministic current-table context remains independent")

    impossible = dict(two_context)
    impossible["whole_table_facts"] = {**two_context["whole_table_facts"], "screen_purpose": "X" * 20000}
    diagnostics = {}
    try:
        fit_analysis_context(impossible, 9000, diagnostics=diagnostics)
    except ValueError as exc:
        assert str(exc) == "context_budget_exceeded"
        assert diagnostics["reduced_chars"] > 9000
        assert diagnostics["section_chars_after"]["whole_table_facts"] > 20000
    else:
        raise AssertionError("irreducible context must be blocked")
    print("PASS irreducible evidence blocked with section diagnostics")


if __name__ == "__main__":
    main()

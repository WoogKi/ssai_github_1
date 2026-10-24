"""DB-free sales-analysis budget regression through the final model message."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import _build_sims_analysis_context_from_df
from app.ui.current_table_followups.analysis_facts import build_verified_group_table, fit_analysis_context
from tools.check_llm_analysis_context_fix_20261022 import runtime


def sales_context() -> dict:
    count = 300
    frame = pd.DataFrame({
        "매출처명": [f"거래처{index % 8}" for index in range(count)],
        "제품코드": [f"{index:05d}" for index in range(count)],
        "제품명": [f"품목{index}" for index in range(count)],
        "기준월": ["202609"] * count,
    })
    for index, name in enumerate((
        "출고수량", "매출공급가액", "매출세액", "매출합계", "전월매출액",
        "3개월매출액", "증감금액", "평균매출액",
    )):
        frame[name] = [index * 100 + row % 17 for row in range(count)]
    for index in range(46 - len(frame.columns)):
        frame[f"참고항목{index}"] = f"값{index}"
    params = {
        "date_from": "20260401", "date_to": "20260930",
        "month_from": "202604", "month_to": "202609",
        "stock_cd_list": ["00001", "00247", "00901"],
        "stock_cds": ["00001", "00247", "00901"],
        "io_gu_list": [f"{index:03d}" for index in range(35)],
        "product_di_list": [f"{index:03d}" for index in range(7)],
        "product_class_list": [f"{index:03d}" for index in range(4)],
        "_period_source_policy": {
            "evaluation_month": "202610", "source_mode": "monthly_book",
            "diagnostic_trace": [f"판정단계{index:03d}" for index in range(345)],
        },
    }
    result = _build_sims_analysis_context_from_df(
        frame, result={}, action_name="품목별 매출 추세 요약표", params=params,
        meta={"display_row_count": 300, "analysis_row_count": count},
    )
    result["table_key"] = "sales-fixture"
    return result


def test_sales_budget() -> None:
    ctx = sales_context()
    source = json.dumps(ctx, ensure_ascii=False, default=str)
    diagnostics = {}
    try:
        fitted = fit_analysis_context(ctx, 12000, diagnostics=diagnostics)
    except ValueError:
        print("SYNTHETIC_BASELINE", len(source), diagnostics)
        raise
    assert json.dumps(ctx, ensure_ascii=False, default=str) == source
    assert fitted["params"]["date_from"] == "20260401"
    assert fitted["params"]["date_to"] == "20260930"
    assert fitted["params"]["stock_cd_list"] == ["00001", "00247", "00901"]
    for name, stats in ctx["whole_table_facts"]["numeric_metrics"].items():
        assert fitted["whole_table_facts"]["numeric_metrics"][name]["sum"] == stats["sum"]

    env, state, warnings, calls = runtime(16384)
    env["_run_sims_llm_analysis_from_fragment"](
        "매출 추세에서 눈여겨볼 점을 알려줘", analysis_ctx_override=ctx,
        clicked_table_key="sales-fixture", clicked_action="품목별 매출 추세 요약표",
    )
    assert not warnings and len(calls) == 1, (warnings, state.get("__sims_analysis_attach_error"))
    system = calls[0]["messages_for_ai"][0]["content"]
    payload = system.split("[SIMS_JSON]\n", 1)[1].split("\n[/SIMS_JSON]", 1)[0]
    delivered = json.loads(payload)
    assert len(payload) <= 12000
    assert delivered["table_key"] == "sales-fixture"
    assert delivered["row_count"] == 300
    assert delivered["display_row_count"] == 300
    assert delivered["whole_table_facts"]["group_consistency_pass"] is True
    print(f"PASS sales context {len(source)}->{len(payload)} chars; stage={diagnostics['stage']}")

    tight = fit_analysis_context(ctx, 9000, question="매출 추세를 분석해줘")
    tight_raw = json.dumps(tight, ensure_ascii=False, default=str, separators=(",", ":"))
    assert len(tight_raw) <= 9000
    assert tight["params"]["date_from"] == "20260401"
    assert tight["whole_table_facts"]["numeric_metrics"]["매출합계"]["sum"] == ctx["whole_table_facts"]["numeric_metrics"]["매출합계"]["sum"]
    print(f"PASS 9000-char budget {len(tight_raw)} chars")

    large_filters = deepcopy(ctx)
    large_filters["params"].pop("_period_source_policy")
    large_filters["params"]["product_group_list"] = [f"G{index:04d}" for index in range(310)]
    large_filters["table_key"] = "sales-large-filter"
    source_filters = json.dumps(large_filters, ensure_ascii=False, default=str)
    filter_diagnostics = {}
    fitted_filters = fit_analysis_context(
        large_filters, 12000, question="매출합계의 거래처별 차이는?",
        diagnostics=filter_diagnostics,
    )
    env, state, warnings, calls = runtime(16384)
    env["_run_sims_llm_analysis_from_fragment"](
        "매출합계의 거래처별 차이는?", analysis_ctx_override=large_filters,
        clicked_table_key="sales-large-filter", clicked_action="품목별 매출 추세 요약표",
    )
    assert not warnings and len(calls) == 1, (warnings, state.get("__sims_analysis_attach_error"))
    raw = calls[0]["messages_for_ai"][0]["content"].split("[SIMS_JSON]\n", 1)[1].split("\n[/SIMS_JSON]", 1)[0]
    filtered = json.loads(raw)
    assert len(raw) <= 12000
    assert raw == json.dumps(fitted_filters, ensure_ascii=False, default=str, separators=(",", ":"))
    assert len(raw) == filter_diagnostics["reduced_chars"]
    assert filtered["params"]["product_group_list"] == large_filters["params"]["product_group_list"]
    assert filtered["whole_table_facts"]["numeric_metrics"]["매출합계"]["group_values_complete"] is True
    omitted = filtered["delivery_scope"]["group_breakdowns_omitted"]
    assert omitted and "매출합계" not in omitted
    assert all(not filtered["whole_table_facts"]["numeric_metrics"][name]["group_values_complete"] for name in omitted)
    assert "매출합계" in build_verified_group_table(filtered["whole_table_facts"])
    assert json.dumps(large_filters, ensure_ascii=False, default=str) == source_filters
    print(f"PASS effective filters and requested metric {len(source_filters)}->{len(raw)} chars; stage={filter_diagnostics['stage']}")


if __name__ == "__main__":
    test_sales_budget()

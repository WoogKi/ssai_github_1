"""DB-free regression for grounded, readable SIMS LLM analysis output."""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys
from time import perf_counter

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import _build_sims_analysis_context_from_df
from app.ui.current_table_followups.analysis_facts import (
    build_verified_group_table,
    compose_verified_analysis_output,
)
from tools.check_llm_analysis_verified_output_20261022 import EXCEL, SOURCE, saved_message


def context(frame: pd.DataFrame) -> dict:
    return _build_sims_analysis_context_from_df(
        frame, result={}, action_name="현재고 조회",
        params={"stock_cd": "00901"}, meta={"display_row_count": 300},
    )


def main() -> None:
    frame = pd.read_excel(EXCEL)
    assert frame.shape == (615, 23)
    facts = context(frame)["whole_table_facts"]
    table = build_verified_group_table(facts, display_rows=300)
    assert "| F·X·자료부족 소계 | 22 | 3.58% | 593 | 4.76% | 4,070,092 | 0.88% |" in table
    assert "| 전체 | 615 | 100.00% | 12,456 | 100.00% | 460,022,038 | 100.00% |" in table

    model_text = "\n".join((
        "## 재고 수량 및 금액 현황",
        "최근 3개월 출고 현황을 함께 확인하세요.",
        "B등급의 보험금액 집중 현상을 점검해야 합니다.",
        "B등급 보험금액 비중은 41.25%이고 보험금액은 189,770,118원입니다.",
        "E등급 제품 수 비중은 37.89%이나 재고수량 비중은 14.68%입니다.",
        "제품코드 00846과 40mg 규격은 별도로 재고 회전을 확인하세요.",
        "## 출고빈도등급별 분포",
        "B등급 보험금액 비중은 41.25%입니다. E등급 보험금액 비중은 37.89%입니다.",
        "E등급 보험금액 비중이 가장 높습니다.",
        "기타 22건에 579개, 2,850,884원이 있습니다.",
        "| 등급 | 건수 |",
        "| B | 30 |",
        "## 관리 제안",
        "보험금액이 집중된 그룹의 재고 회전과 보충 주기를 확인하세요.",
    ))
    t0 = perf_counter()
    output = compose_verified_analysis_output(table, model_text, facts=facts)
    elapsed_ms = (perf_counter() - t0) * 1000
    for expected in ("최근 3개월", "B등급의 보험금액 집중", "41.25%", "189,770,118원",
                     "제품 수 비중은 37.89%", "재고수량 비중은 14.68%", "제품코드 00846", "40mg",
                     "재고 회전과 보충 주기"):
        assert expected in output, expected
    for unsupported in ("E등급 보험금액 비중은 37.89%", "E등급 보험금액 비중이 가장 높",
                        "579개", "2,850,884원", "| B | 30 |"):
        assert unsupported not in output, unsupported
    assert output.count("**검증된 전체 집계**") == 1
    assert output.startswith(table)
    assert "## 관리 제안" in output
    assert saved_message(table, facts=facts, model_text=model_text)["content"] == output

    heading_cases = (
        ("## E등급 보험금액 비중은 99.99%", False),
        ("## E등급 보험금액 비중은 13.50%", True),
        ("**E등급 보험금액 비중은 99.99%**", False),
        ("1. E등급 보험금액 비중은 99.99%", False),
        ("1. E등급 보험금액 비중은 13.50%", True),
        ("## 출고빈도등급별 분포", True),
        ("## 최근 3개월 출고 현황", True),
        ("## E등급 보험금액 비중이 가장 높습니다", False),
    )
    for heading, expected in heading_cases:
        rendered = compose_verified_analysis_output(
            table, heading + "\n금액 집중도를 확인하세요.", facts=facts)
        assert (heading in rendered[len(table):]) is expected, heading
        assert rendered.startswith(table) and "금액 집중도를 확인하세요." in rendered
        if not expected:
            assert "\n1.\n" not in rendered[len(table):], heading
    for title in ("제품코드 00846", "40mg 규격"):
        rendered = compose_verified_analysis_output(
            table, "## " + title + "\n재고 회전을 확인하세요.", facts=facts)
        assert "## " + title in rendered
    saved = saved_message(
        table, facts=facts,
        model_text="## E등급 보험금액 비중은 99.99%\n## 출고빈도등급별 분포\n금액 집중도를 확인하세요.",
    )["content"]
    assert saved.startswith(table) and "99.99%" not in saved
    assert "## 출고빈도등급별 분포\n금액 집중도를 확인하세요." in saved

    headings_only = compose_verified_analysis_output(
        table, "재고 수량 및 금액 현황\n출고빈도등급별 분포\n보험금액 집중도", facts=facts)
    assert "E 그룹이 가장 많습니다" in headings_only and "관리 우선순위" in headings_only
    assert "출고빈도등급별 분포" not in headings_only[len(table):]

    synthetic_703 = pd.concat([frame, frame.iloc[:88]], ignore_index=True)
    facts_703 = context(synthetic_703)["whole_table_facts"]
    table_703 = build_verified_group_table(facts_703, display_rows=300)
    assert "전체 703건 기준 (화면 표시 300건)" in table_703
    model_703 = "전체 703건의 최근 3개월 출고를 확인하세요. 전체 615건으로 설명하면 안 됩니다."
    result_703 = saved_message(table_703, facts=facts_703, model_text=model_703)["content"]
    assert "전체 703건의 최근 3개월" in result_703
    assert "전체 615건으로" not in result_703

    tree = ast.parse(SOURCE.read_text(encoding="utf-8-sig"))
    imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)
               and node.module == "app.ui.current_table_followups.analysis_facts"]
    assert imports and {"build_verified_group_table", "compose_verified_analysis_output"}.issubset(
        {alias.name for alias in imports[0].names})
    fresh = subprocess.run(
        [sys.executable, "-c", "from app.ui.current_table_followups.analysis_facts import "
         "build_verified_group_table, compose_verified_analysis_output; print('IMPORT_PASS')"],
        cwd=SOURCE.parents[1], capture_output=True, text=True, check=True,
    )
    assert "IMPORT_PASS" in fresh.stdout
    print(f"PASS Excel 615, synthetic 703, grounded prose, saved/rendered output, import; compose_ms={elapsed_ms:.3f}")


if __name__ == "__main__":
    main()

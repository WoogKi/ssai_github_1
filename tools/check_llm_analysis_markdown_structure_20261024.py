"""DB-free final-answer regression using Markdown shapes seen in the UI."""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.current_table_followups.analysis_facts import (
    build_whole_table_facts, compose_verified_analysis_output,
)
from tools.check_llm_analysis_verified_output_20261022 import saved_message


def main() -> None:
    facts = build_whole_table_facts(
        pd.DataFrame({"추세판정": ["증가", "유지"], "총매출액": [100, 200]}),
        action="품목별 매출 추세 요약표", query="2026-10 매출 추세",
    )
    cases = [
        (
            "## 1. 조회 개요\n조회 기간을 확인하세요.\n"
            "## 3. 관리 제안\n원전표를 확인하세요.",
            ("## 1. 조회 개요", "## 2. 관리 제안"),
            ("## 3. 관리 제안",),
        ),
        (
            "**4. 관리 제안**\n원전표를 확인하세요.",
            ("**1. 관리 제안**",),
            ("**4. 관리 제안**",),
        ),
        (
            "- **매출액 기준 상위 품목:**\n확인된 전체 합계를 검토하세요.",
            ("확인된 전체 합계를 검토하세요.",),
            ("- **매출액 기준 상위 품목:**",),
        ),
    ]
    for source, keep, reject in cases:
        output = compose_verified_analysis_output("검증표", source, facts=facts)
        for expected in keep:
            assert expected in output, (source, output)
        for forbidden in reject:
            assert forbidden not in output, (source, output)
        saved = saved_message("검증표", facts=facts, model_text=source)["content"]
        assert saved == output

    combined = (
        "1. 조회 개요\n조회 조건을 확인하세요.\n"
        "## 2. 빈 절\n## 4. 관리 제안\n"
        "- **상위 품목:**\n- 다음과 같습니다.\n"
        "- **확인 대상:**\n  - 원전표의 수량을 대조하세요.\n"
        "  1. 입고 상태를 확인하세요.\n  2. 납기일을 확인하세요.\n"
        "### 3. 후속 조치\n담당자에게 확인하세요.\n"
        "**5. 추가 확인**\n확정 금액을 확인하세요."
    )
    output = compose_verified_analysis_output("검증표", combined, facts=facts)
    for expected in ("조회 개요", "관리 제안", "확인 대상", "원전표의 수량",
                     "1. 입고 상태", "2. 납기일", "후속 조치", "추가 확인"):
        assert expected in output, output
    for forbidden in ("빈 절", "- **상위 품목:**", "다음과 같습니다.", "## 4.", "**5."):
        assert forbidden not in output, output
    assert saved_message("검증표", facts=facts, model_text=combined)["content"] == output
    numbered = (
        "## 1. 확인 사항\n"
        "1. 원전표 확인\n"
        "2. 납기일 확인\n"
        "## 3. 관리 제안\n"
        "확정 금액을 대조하세요."
    )
    output = compose_verified_analysis_output("검증표", numbered, facts=facts)
    for expected in ("## 1. 확인 사항", "1. 원전표 확인", "2. 납기일 확인",
                     "## 2. 관리 제안", "확정 금액을 대조하세요."):
        assert expected in output, output
    assert saved_message("검증표", facts=facts, model_text=numbered)["content"] == output
    unsupported_parent = (
        "- **매출액 비중 99.99%:**\n"
        "  - 원전표의 금액을 확인하세요."
    )
    output = compose_verified_analysis_output("검증표", unsupported_parent, facts=facts)
    assert "99.99%" not in output
    assert "원전표의 금액을 확인하세요." in output
    assert saved_message("검증표", facts=facts, model_text=unsupported_parent)["content"] == output
    screen_text = (
        "## 1. 조회 개요\n전체 합계를 확인하세요.\n"
        "## 2. 주요 특징\n"
        "- **금액 기준 상위 매입처:** 1. 2. 3.\n"
        "  - **수량 관련 주의사항:**\n"
        "- 관리 제안:\n"
        "- **확인 대상:**\n  - 원전표의 수량을 대조하세요.\n"
        "  1. 입고 상태를 확인하세요.\n  2. 납기일을 확인하세요.\n"
        "## 3. 후속 조치\n확정 금액을 대조하세요."
    )
    output = compose_verified_analysis_output("검증표", screen_text, facts=facts)
    for forbidden in ("금액 기준 상위 매입처:** 1. 2. 3.",
                      "수량 관련 주의사항:", "관리 제안:"):
        assert forbidden not in output, output
    for expected in ("전체 합계를 확인하세요.", "확인 대상:",
                     "1. 입고 상태를 확인하세요.", "2. 납기일을 확인하세요.",
                     "## 3. 후속 조치"):
        assert expected in output, output
    assert saved_message("검증표", facts=facts, model_text=screen_text)["content"] == output
    print("PASS Markdown heading/list structure and final display/save")


if __name__ == "__main__":
    main()

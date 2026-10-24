"""DB-free regression for verified operational findings and final message."""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import _build_sims_analysis_context_from_df
from app.ui.current_table_followups.analysis_facts import (
    _verified_interpretation_sentence, build_whole_table_facts,
)
from tools.check_llm_analysis_final_20261023 import _model_payload
from tools.check_llm_analysis_verified_output_20261022 import saved_message


def verify(action: str, frame: pd.DataFrame, *, display_rows: int) -> tuple[dict, str]:
    context = _build_sims_analysis_context_from_df(
        frame, result={}, action_name=action,
        params={"date_from": "20261023", "date_to": "20261023"},
        meta={"display_row_count": display_rows},
    )
    context["table_key"] = "offline-1819-" + action
    delivered = _model_payload(context, action + " 분석")
    assert delivered["row_count"] == len(frame)
    assert delivered["display_row_count"] == display_rows
    facts = delivered["whole_table_facts"]
    model_text = (
        "1. 전체 현황\n"
        "2. 주요 특징\n"
        "**상위 품목:**\n"
        "- 상위 품목:\n"
        "  4.\n"
        "3. 확인 사항\n"
        "거래명세서와 세금계산서가 불일치하며 반품·조정이 원인입니다.\n"
        "원천 전표의 수량과 금액을 확인하세요."
    )
    output = saved_message("", facts=facts, model_text=model_text)["content"]
    assert "거래명세서와 세금계산서가 불일치" not in output
    assert "- 상위 품목:" not in output
    assert "**상위 품목:**" not in output
    assert "\n2.\n" not in output
    assert "\n4.\n" not in output
    assert "원천 전표의 수량과 금액을 확인하세요" in output
    assert "해석 생성 중" not in output
    return facts, output


def real_workbooks(folder: Path) -> None:
    expected = pd.read_excel(folder / "입고예정조회_20261024_181913.xlsx", dtype=object)
    inbound = pd.read_excel(folder / "입고명세_조회_20261024_181836.xlsx", dtype=object)
    outbound = pd.read_excel(folder / "출고명세_조회_20261024_181854.xlsx", dtype=object)

    facts, output = verify("입고예정조회", expected, display_rows=300)
    assert facts["row_count"] == 3174
    assert facts["numeric_metrics"]["미입고수량"]["sum"] == 252071
    assert int(expected["단가적용처명"].isna().sum()) == 0
    misleading = saved_message("", facts=facts, model_text=(
        "단가적용처명이 많이 비어 있습니다. 납기일 상위 잔량의 원전표를 확인하세요."
    ))["content"]
    assert "단가적용처명이 많이 비어" not in misleading
    assert "납기일 상위 잔량" in misleading
    findings = facts["operational_findings"]
    assert findings["pending_quantity"] == 252071
    assert findings["due_date_top"] and findings["vendor_top"] and findings["product_top"]
    assert "납기일" in output and "거래처" in output and "품목" in output
    for kind in ("due_date_top", "vendor_top", "product_top"):
        top = findings[kind][0]
        assert top["name"] in output
        assert f"{top['quantity']:,}개" in output
        assert f"{top['share_pct']:.2f}%" in output

    facts, output = verify("입고명세 조회", inbound, display_rows=200)
    assert facts["row_count"] == 929
    findings = facts["operational_findings"]
    assert findings["document_match"] == {"Y": 929, "N": 0, "unknown": 0}
    assert findings["tax_invoice_comparison_available"] is False
    assert findings["supply_difference_rows"] == 260
    assert findings["supply_difference_sum"] == 30567262
    assert findings["negative_quantity_rows"] == 5
    assert findings["negative_supply_rows"] == 4
    assert "거래명세서 금액 일치" in output
    assert "세금계산서 비교값 없음" in output
    assert "원본·확정 공급가액 차이" in output
    assert "Y 929건" in output and "+30,567,262원" in output
    assert _verified_interpretation_sentence("세금계산서 비교값 없음", facts)
    assert not _verified_interpretation_sentence("세금계산서 비교값 없음", {})
    misleading = saved_message("", facts=facts, model_text=(
        "금액 기준 상위 매입처: 1. 2. 3.\n"
        "수량 관련 주의사항:\n- 관리 제안:\n"
        "원본과 확정 공급가액 차이는 조정이나 계산 방식의 차이로 보입니다.\n"
        "- 확정 금액 검증: 차이 행의 원전표를 대조하세요."
    ))["content"]
    for forbidden in ("상위 매입처: 1. 2. 3.", "수량 관련 주의사항:",
                      "관리 제안:", "조정이나 계산 방식의 차이"):
        assert forbidden not in misleading, misleading
    assert "차이 행의 원전표를 대조하세요." in misleading
    assert "Y 929건" in misleading and "+30,567,262원" in misleading
    assert not _verified_interpretation_sentence(
        "이는 확정 단계의 조정을 시사합니다.", facts)
    assert not _verified_interpretation_sentence(
        "거래명세서 금액 불일치가 260건입니다.", facts)

    facts, output = verify("출고명세 조회", outbound, display_rows=200)
    assert facts["row_count"] == 7113
    findings = facts["operational_findings"]
    assert findings["supply_difference_rows"] == 1
    assert findings["supply_difference_sum"] == -10664
    assert findings["negative_quantity_rows"] == 81
    assert findings["negative_supply_rows"] == 78
    assert findings["vendor_top"] and findings["product_top"]
    assert "원본·확정 공급가액 차이" in output
    assert "81건" in output and "-10,664원" in output
    misleading = saved_message("", facts=facts, model_text=(
        "원본/확정 금액 차이는 반품이나 마감 처리 때문일 수 있습니다. "
        "차이 전표를 확인하세요."
    ))["content"]
    assert "반품이나 마감 처리 때문" not in misleading
    assert "차이 전표를 확인하세요." in misleading
    assert "-10,664원" in misleading

    missing_facts = build_whole_table_facts(
        pd.DataFrame({"단가적용처명": ["계약A", None, "계약B"], "입고수량": [1, 2, 3]}),
        action="입고명세 조회", query="입고명세 조회")
    assert missing_facts["field_missing_counts"]["단가적용처명"] == 1
    assert _verified_interpretation_sentence("단가적용처명 결측 1건입니다.", missing_facts)
    assert not _verified_interpretation_sentence("단가적용처명 결측 2건입니다.", missing_facts)
    partial_facts = {**missing_facts, "analysis_scope": "partial_source"}
    assert not _verified_interpretation_sentence("단가적용처명 결측 1건입니다.", partial_facts)
    assert _verified_interpretation_sentence("확보된 자료에서 단가적용처명 결측 1건입니다.", partial_facts)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        real_workbooks(Path(sys.argv[1]))
        print("PASS actual read-only Excel operational findings")
    else:
        sample = pd.DataFrame({
            "발주거래처명": ["가", "가", "나"],
            "제품명": ["제품A", "제품A", "제품B"],
            "납기일자": ["2026-10-23", "2026-10-23", "2026-10-24"],
            "발주수량": [5, 3, 2], "입고수량": [0, 0, 0],
            "미입고수량": [5, 3, 2],
        })
        facts, output = verify("입고예정조회", sample, display_rows=2)
        assert facts["operational_findings"]["pending_quantity"] == 10
        assert "제품A" in output and "2026-10-23" in output
        print("PASS synthetic operational findings")

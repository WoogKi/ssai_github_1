"""DB-free final-message regression for four SIMS analysis actions."""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import _build_sims_analysis_context_from_df
from app.ui.current_table_followups.analysis_facts import (
    _verified_interpretation_sentence, build_verified_group_table, build_whole_table_facts,
)
from tools.check_llm_analysis_final_20261023 import _model_payload
from tools.check_llm_analysis_verified_output_20261022 import saved_message


def _result(action: str, frame: pd.DataFrame, model_text: str, *, params: dict,
            display_rows: int = 300) -> tuple[str, dict, str]:
    context = _build_sims_analysis_context_from_df(
        frame, result={}, action_name=action, params=params,
        meta={"display_row_count": display_rows},
    )
    context["table_key"] = "offline-" + action
    delivered = _model_payload(context, action + " 분석")
    assert delivered["row_count"] == len(frame)
    assert delivered["display_row_count"] == display_rows
    facts = delivered["whole_table_facts"]
    table = build_verified_group_table(
        facts, scope=delivered["query_scope_summary"], display_rows=display_rows)
    output = saved_message(table, facts=facts, model_text=model_text)["content"]
    assert "해석 생성 중" not in output
    assert "\n1.\n" not in output and "\n2.\n" not in output
    assert f"전체 {len(frame):,}건" in output
    return output, facts, table


def sales_trend() -> None:
    rows = 10020
    total_sales = 170411511981
    groups = ([("안정", 5164), ("미지정", 1540), ("증가", 1166),
               ("감소", 942), ("신규/증가", 845), ("자료부족", 185),
               ("반품주의", 178)])
    frame = pd.DataFrame({
        "추세판정": [name for name, count in groups for _ in range(count)],
        "총매출액": [total_sales // rows] * rows,
        "총매출세액": [10] * rows,
        "총출고수량": [7173219 // rows] * rows,
    })
    frame.loc[0, "총매출액"] += total_sales % rows
    frame.loc[0, "총출고수량"] += 7173219 % rows
    output, facts, table = _result(
        "품목별 매출 추세 요약표", frame,
        "1. 전체 현황\n전체 10,020건입니다.\n"
        f"매출액 합계는 {total_sales:,}원입니다.\n"
        "총매출액 합계는 9,999원입니다.\n"
        "2. 매출 추세 분포 분석\n안정 그룹 총매출액은 9,999원입니다.\n"
        "3. 확인 사항\n매출 추세와 원천 거래를 비교하세요.",
        params={"date_from": "20260401", "date_to": "20260930"},
    )
    assert facts["numeric_metrics"]["총매출액"]["sum"] == total_sales
    assert _verified_interpretation_sentence(
        f"매출액 합계는 {total_sales:,}원입니다.", facts)
    assert not _verified_interpretation_sentence("매출액 합계는 9,999원입니다.", facts)
    assert "170,411,511,981" in table and "170,411,511,981원" in output[len(table):]
    assert f"매출액 합계는 {total_sales:,}원" in output[len(table):]
    assert "안정 5,164건" in output[len(table):]
    assert "9,999원" not in output
    assert "2. 매출 추세 분포 분석" not in output[len(table):]
    assert "2. 확인 사항" in output, output


def inbound_expected() -> None:
    rows = 3174
    frame = pd.DataFrame({
        "발주처명": [f"거래처{i % 17}" for i in range(rows)],
        "발주수량": [3] * rows,
        "입고수량": [1] * rows,
        "미입고수량": [2] * rows,
    })
    output, facts, table = _result(
        "입고예정조회", frame,
        "1. 현황\n미입고수량 합계는 99,999개입니다.\n"
        "2. 주요 분포 및 특징\n1.\n3. 확인 사항\n"
        "미입고 상태의 원천 전표를 확인하세요.",
        params={"date_from": "20261020", "date_to": "20261023"},
    )
    assert not table and facts["group_count"] == 17
    assert "미입고수량 합계 6,348" in output
    assert "99,999" not in output
    assert "2. 주요 분포 및 특징" not in output
    assert "1. 확인 사항" in output, output


def orders() -> None:
    rows = 15438
    frame = pd.DataFrame({
        "발주거래처명": [f"발주처{i % 3}" for i in range(rows)],
        "발주수량": [2] * rows,
        "입고수량": [1] * rows,
        "미입고수량": [1] * rows,
    })
    output, _, table = _result(
        "발주조회", frame,
        "1. 개요\n발주수량은 99,999개입니다.\n"
        "2. 주요 수치 분석\n미입고수량은 88,888개입니다.\n"
        "3. 관리 제안\n전표와 입고 상태를 대조하세요.",
        params={"date_from": "20261023", "date_to": "20261023"},
    )
    assert table
    assert "발주수량 합계 30,876" in output[len(table):]
    assert "99,999" not in output and "88,888" not in output
    assert "2. 주요 수치 분석" not in output[len(table):]


def products() -> None:
    rows = 10260
    frame = pd.DataFrame({
        "출고빈도등급": ["E" if i % 2 else "A" for i in range(rows)],
        "추정기여금액": [100] * rows,
        "3개월출고발생수": [1] * rows,
    })
    output, _, table = _result(
        "제품정보 조회", frame,
        "1. 데이터 개요\n전체 10,260건입니다.\n"
        "2. 손익 현황\nE등급 추정기여금액은 99,999원입니다.\n"
        "3. 확인 사항\n자료부족 제품의 근거를 점검하세요.",
        params={"evaluation_month": "202610"},
    )
    assert table
    assert "추정기여금액 합계 1,026,000원" in output[len(table):]
    assert "99,999원" not in output
    assert "2. 손익 현황" not in output[len(table):]


def expected_receipts_real_shape() -> None:
    rows = 3174
    pending = [80 if index < 1325 else 79 for index in range(rows)]
    assert sum(pending) == 252071
    frame = pd.DataFrame({
        "발주일자": ["20261023"] * rows,
        "제품명": [f"품목{index:04d}" for index in range(rows)],
        "발주거래처명": [f"거래처{index % 17}" for index in range(rows)],
        "발주수량": pending,
        "할증수량": [0] * rows,
        "입고수량": [0] * rows,
        "미입고수량": pending,
    })
    context = _build_sims_analysis_context_from_df(
        frame, result={}, action_name="입고예정조회",
        params={"month_from": "202609", "month_to": "202610"},
        meta={"display_row_count": 300},
    )
    context["table_key"] = "offline-expected-3174"
    delivered = _model_payload(context, "입고예정자료 분석")
    assert delivered["row_count"] == rows and delivered["display_row_count"] == 300
    assert delivered["business_terms"]["qty_source_column"] == "미입고수량"
    assert delivered["sales_group_profile"]["qty_col"] == "미입고수량"
    assert delivered["sales_group_profile"]["product_quantity_top"]
    facts = delivered["whole_table_facts"]
    assert list(facts["numeric_metrics"])[:3] == ["발주수량", "입고수량", "미입고수량"], ascii(list(facts["numeric_metrics"]))
    assert facts["numeric_metrics"]["발주수량"]["sum"] == 252071
    assert facts["numeric_metrics"]["입고수량"]["sum"] == 0
    assert facts["numeric_metrics"]["미입고수량"]["sum"] == 252071
    model_text = (
        "1. 조회 개요\n전체 3,174건입니다.\n"
        "2. 주요 수치\n입고수량 합계는 0개입니다.\n"
        "미입고수량 합계는 0개입니다.\n"
        "4. 주요 특징\n"
        "② 품목 특이사항\n"
        "product_quantity_top 분석 결과, 다수의 품목이 0개입니다.\n"
        "품목명에 (~26.10월말품절), (~6월 초) 등 납기 정보가 포함됩니다.\n"
        "3. 확인 사항\n원천 전표의 입고 상태를 확인하세요."
    )
    output = saved_message("", facts=facts, model_text=model_text)["content"]
    assert "발주수량 합계 252,071" in output
    assert "입고수량 합계는 0개" in output, output
    assert "미입고수량 합계 252,071" in output
    assert "입고수량 합계는 0개" in output
    assert "미입고수량 합계는 0개" not in output
    assert "product_quantity_top" not in output and "~26.10" not in output
    assert "다수의 품목" not in output
    assert "3. 확인 사항" in output and "4. 주요 특징" not in output
    assert "해석 생성 중" not in output


def distinct_money_and_cause() -> None:
    frame = pd.DataFrame({
        "제품명": ["제품가", "제품나"],
        "수량": [2, 3],
        "공급가액": [100, 200],
        "확정공급가액": [90, 180],
        "세액": [10, 20],
        "확정세액": [9, 18],
    })
    facts = build_whole_table_facts(frame, action="출고명세 조회", query="어제 출고현황")
    for name, value in (("공급가액", 300), ("확정공급가액", 270),
                        ("세액", 30), ("확정세액", 27)):
        assert facts["numeric_metrics"][name]["sum"] == value
        assert _verified_interpretation_sentence(f"전체 {name} 합계는 {value}원입니다.", facts), name
    assert not _verified_interpretation_sentence("전체 확정공급가액 합계는 300원입니다.", facts)
    assert not _verified_interpretation_sentence("전체 확정세액 합계는 30원입니다.", facts)
    assert not _verified_interpretation_sentence("총 매출세액은 30원입니다.", facts)
    assert not _verified_interpretation_sentence("음수 금액은 반품·조정 가능성이 있습니다.", facts)
    assert not _verified_interpretation_sentence(
        "이는 단순 반품 처리가 아니며 입력 오류나 조정 건일 가능성이 있습니다.", facts)
    assert _verified_interpretation_sentence("음수 금액의 원천 전표를 확인하세요.", facts)


if __name__ == "__main__":
    sales_trend()
    inbound_expected()
    orders()
    products()
    expected_receipts_real_shape()
    distinct_money_and_cause()
    print("PASS four actions: model payload, verified prose, final display and saved message")

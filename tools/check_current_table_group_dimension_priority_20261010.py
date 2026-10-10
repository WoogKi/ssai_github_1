"""DB-free regression for explicit Current Table product dimensions."""

from __future__ import annotations

import logging
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui.current_table_followups.action_dispatcher import (
    build_current_table_interpretive_facts,
    handle_current_table_followup_by_action,
)
from app.ui.current_table_followups.generic import (
    _find_common_group_column,
    handle_common_column_group_followup,
)


def _aggregate(df: pd.DataFrame, query: str, action: str) -> tuple[bool, dict]:
    captured: dict = {}

    def push_table(**kwargs: object) -> bool:
        captured.update(kwargs)
        return True

    handled = handle_common_column_group_followup(
        df=df,
        query=query,
        top_n=20,
        table_key="original",
        source_action=action,
        helpers={"push_table": push_table},
        log=logging.getLogger(__name__),
    )
    return handled, captured


def main() -> int:
    rows = 9418
    source = pd.DataFrame({
        "제품코드": [f"{index:05d}" for index in range(rows)],
        "제품명": [f"제품 {index // 2}" for index in range(rows)],
        "구분명": ["보험", "비보험"] * (rows // 2),
        "제품분류명": ["의약품", "기타"] * (rows // 2),
        "제품그룹명": ["처방", "일반"] * (rows // 2),
        "재고수량": [1, 2] * (rows // 2),
    })
    cases = (
        ("현재표 제품별 집계", "제품명", rows // 2),
        ("현재표 제품명별 집계", "제품명", rows // 2),
        ("현재표 제품구분별 집계", "구분명", 2),
        ("현재표 구분별 집계", "구분명", 2),
        ("현재표 제품분류별 집계", "제품분류명", 2),
        ("현재표 제품그룹별 집계", "제품그룹명", 2),
    )
    for query, column, expected_groups in cases:
        assert _find_common_group_column(source, query, "제품정보 조회") == column, query
        handled, result = _aggregate(source, query, "제품정보 조회")
        assert handled, query
        assert result["extra_meta"]["group_column"] == column, query
        assert result["extra_meta"]["source_row_count"] == rows, query
        assert result["source_table_key"] == "original", query
        assert len(result["df"]) == expected_groups, (query, len(result["df"]))

    routed: dict = {}
    assert handle_current_table_followup_by_action(
        df=source,
        query="현재표 제품구분별 집계",
        top_n=20,
        table_key="original",
        source_action="제품정보 조회",
        helpers={
            "push_table": lambda **kwargs: routed.update(kwargs) or True,
            "push_notice": lambda **kwargs: routed.update(kwargs) or True,
            "find_col": lambda frame, names: next((name for name in names if name in frame), ""),
            "to_num": lambda values: pd.to_numeric(values, errors="coerce"),
            "add_seq": lambda frame: frame,
            "fmt_num": str,
        },
        log=logging.getLogger(__name__),
    )
    assert routed["extra_meta"]["group_column"] == "구분명", routed
    assert len(routed["df"]) == 2, routed
    assert routed["action"] == "현재표 구분명별 집계", routed

    for query, column in (
        ("현재표 제품구분별 분석", "구분명"),
        ("현재표 구분별 분석", "구분명"),
        ("현재표 제품분류별 분석", "제품분류명"),
        ("현재표 제품그룹별 분석", "제품그룹명"),
    ):
        facts = build_current_table_interpretive_facts(
            df=source, query=query, source_action="제품정보 조회",
        )
        assert facts["status"] == "success", (query, facts)
        assert facts["input_row_count"] == rows, query
        assert facts["whole_table_facts"]["group_column"] == column, query

    missing = source.drop(columns=["제품분류명"])
    assert not _aggregate(missing, "현재표 제품분류별 집계", "제품정보 조회")[0]
    assert build_current_table_interpretive_facts(
        df=missing, query="현재표 제품분류별 분석", source_action="제품정보 조회",
    )["status"] == "column_unavailable"

    inventory = pd.DataFrame({
        "제품코드": ["1", "2"], "제품명": ["A", "B"],
        "제품그룹명": ["처방", "일반"], "재고수량": [3, 4],
    })
    assert _find_common_group_column(inventory, "현재표 제품그룹별 집계", "제품재고장 조회") == "제품그룹명"
    assert _find_common_group_column(inventory, "현재표 제품명별 집계", "제품재고장 조회") == "제품명"
    assert not _aggregate(inventory, "현재표 제품분류별 집계", "제품재고장 조회")[0]

    document = pd.DataFrame({
        "제품코드": ["1", "2"], "제품명": ["A", "B"], "구분명": ["매출", "매입"],
    })
    assert not _aggregate(document, "현재표 제품구분별 집계", "거래명세서 공통 조회")[0]
    assert _find_common_group_column(document, "현재표 구분별 집계", "거래명세서 공통 조회") == "구분명"
    print("PASS: six product aggregates, four analyses, missing-column and other-source boundaries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

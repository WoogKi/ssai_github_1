"""DB-free product-information staff grouping and interpretation contract."""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui.current_table_followups.action_dispatcher import build_current_table_interpretive_facts
from app.ui.current_table_followups.analysis_facts import fit_analysis_context
from app.ui.current_table_followups.generic import _build_common_group_summary, _find_common_group_column


def main() -> int:
    total = 10172
    assigned = [f"담당자{index:02d}" for index in range(16) for _ in range(378)]
    assigned += [f"담당자{index:02d}" for index in range(4)]
    rows = pd.DataFrame({
        "제품코드": [f"{index:05d}" for index in range(total)],
        "발주처명": ["거래처"] * total,
        "발주처 담당자": [""] * 4120 + assigned,
        "제약사": ["제약사"] * total,
        "제약사 담당자": ["제약담당"] * (total // 2) + [""] * (total // 2),
        "3개월출고수량": [index % 9 for index in range(total)],
        "3개월반품공급가액": [index % 17 for index in range(total)],
        "평균매출단가": [100] * total,
    })
    assert len(rows) == total and rows["제품코드"].is_unique
    queries = (
        ("현재표 발주처 담당자별 집계", "발주처 담당자"),
        ("현재표 발주처 담당자별 분석", "발주처 담당자"),
        ("현재표 발주처담당자 분석", "발주처 담당자"),
        ("현재표 발주처담당자별 분석", "발주처 담당자"),
        ("현재표 제약사 담당자별 집계", "제약사 담당자"),
        ("현재표 제약사 담당자 분석", "제약사 담당자"),
        ("현재표 제약사담당자 분석", "제약사 담당자"),
    )
    for query, column in queries:
        assert _find_common_group_column(rows, query, "제품정보 조회") == column
        if "집계" in query:
            continue
        result = build_current_table_interpretive_facts(
            df=rows, query=query, source_action="제품정보 조회")
        assert result["status"] == "success", (query, result)
        assert result["input_row_count"] == result["source_row_count"] == total
        whole = result["whole_table_facts"]
        delivered = fit_analysis_context({"whole_table_facts": whole}, 9000)["whole_table_facts"]
        assert delivered["group_rows_total"] == total
        assert whole["group_column"] == column and result["fact_row_count"] > 0
        aggregate = _build_common_group_summary(rows, column, source_action="제품정보 조회")
        assert result["fact_row_count"] == len(aggregate)
        assert whole["group_rows_total"] == aggregate["제품수"].sum() == total
        distribution = {item["group"]: item for item in whole["group_distribution"]}
        assert len(distribution) == len(aggregate)
        for _index, entry in aggregate.iterrows():
            group = "미지정" if str(entry[column]) in {"미지정", "(미지정)"} else str(entry[column])
            assert distribution[group]["rows"] == entry["제품수"], (query, group)
        for metric in ("3개월출고수량", "3개월반품공급가액"):
            facts = whole["numeric_metrics"][metric]
            assert facts["group_sum_matches_total"] is True
            assert facts["sum"] == aggregate[metric].sum()
        assert "제품코드" not in whole["numeric_metrics"]
        assert whole["numeric_metrics"]["평균매출단가"]["aggregation"] == "mean"
        if column == "발주처 담당자":
            assert result["fact_row_count"] == 17
            assert distribution["미지정"]["rows"] == 4120
            assert round(distribution["미지정"]["row_share_pct"], 2) == 40.50

    ambiguous = build_current_table_interpretive_facts(
        df=rows, query="현재표 담당자 분석", source_action="제품정보 조회")
    assert ambiguous["status"] == "input_required"
    assert "ambiguous_staff_dimension" in ambiguous["capability"]["issue_codes"]
    missing = build_current_table_interpretive_facts(
        df=rows.drop(columns=["발주처 담당자"]), query="현재표 발주처담당자 분석",
        source_action="제품정보 조회")
    assert missing["status"] == "column_unavailable"
    other = build_current_table_interpretive_facts(
        df=rows.drop(columns=["발주처 담당자"]), query="현재표 제약사담당자 분석",
        source_action="제품정보 조회")
    assert other["status"] == "success" and other["whole_table_facts"]["group_column"] == "제약사 담당자"
    print("PASS staff dimensions, 10172 rows/17 groups, missing share, sums, ambiguity, and missing columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

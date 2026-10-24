"""DB-free full-source facts regression for the 2026-10-22 current-stock case."""

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.chat_middleware import _build_sims_analysis_context_from_df


GRADE_TOTALS = {
    "A": (23, 470, 10548872),
    "B": (61, 851, 12523750),
    "C": (103, 1755, 32962730),
    "D": (143, 1561, 45744053),
    "E": (251, 2128, 67716876),
    "F": (5, 19, 1575590),
    "X": (10, 43, 639870),
    "빈도자료 부족": (4, 521, 2215772),
}


def stock_fixture() -> pd.DataFrame:
    rows = []
    zero_rows = {"C": 1, "D": 1, "E": 2, "빈도자료 부족": 1}
    for grade, (count, quantity, amount) in GRADE_TOTALS.items():
        zero_count = zero_rows.get(grade, 0)
        for index in range(count):
            is_zero = 0 < index <= zero_count
            rows.append({
                "제품코드": f"{len(rows):05d}", "제품명": f"fixture-{len(rows)}",
                "출고빈도등급": grade,
                "재고수량": quantity - count + 1 if index == 0 else 1,
                "보험금액": amount - (count - zero_count - 1) if index == 0 else (0 if is_zero else 1),
                "Rd06_Password": "fixture-secret",
            })
    return pd.DataFrame(rows)


def build_context(frame: pd.DataFrame) -> dict:
    return _build_sims_analysis_context_from_df(
        frame, result={}, action_name="현재고 조회", params={"stock_cd": "00901"},
        meta={"display_row_count": 300, "analysis_row_count": len(frame)},
    )


def assert_full_breakdown(frame: pd.DataFrame) -> dict:
    ctx = build_context(frame)
    facts = ctx["whole_table_facts"]
    assert ctx["row_count"] == facts["row_count"] == 600
    assert ctx["display_row_count"] == 300
    assert facts["group_count"] == 8 and facts["group_rows_total"] == 600
    assert facts["group_consistency_pass"] is True
    quantity = facts["numeric_metrics"]["재고수량"]
    insurance = facts["numeric_metrics"]["보험금액"]
    assert quantity["sum"] == 7348 and insurance["sum"] == 173927513
    assert quantity["group_sum_matches_total"] and insurance["group_sum_matches_total"]
    assert quantity["group_values_complete"] and insurance["group_values_complete"]
    by_quantity = {row["group"]: row for row in quantity["group_values"]}
    by_insurance = {row["group"]: row for row in insurance["group_values"]}
    assert set(by_quantity) == set(by_insurance) == set(GRADE_TOTALS)
    for grade, (count, qty, amount) in GRADE_TOTALS.items():
        assert by_quantity[grade]["rows"] == count
        assert by_quantity[grade]["value"] == qty
        assert by_insurance[grade]["value"] == amount
    assert sum(row["rows"] for row in by_quantity.values()) == 600
    assert sum(row["value"] for row in by_quantity.values()) == 7348
    assert sum(row["value"] for row in by_insurance.values()) == 173927513
    assert insurance["zero_count"] == 5 and insurance["missing_or_invalid_count"] == 0
    assert ctx["business_terms"]["amount_label"] == "보험금액"
    assert "fixture-secret" not in json.dumps(ctx, ensure_ascii=False, default=str)
    return ctx


def main() -> None:
    fixture = stock_fixture()
    before = fixture.copy(deep=True)
    assert_full_breakdown(fixture)
    pd.testing.assert_frame_equal(fixture, before)
    print("PASS synthetic 600 rows: all eight grade counts, quantities, insurance amounts")

    missing = fixture.copy()
    missing.loc[0, "보험금액"] = None
    money = build_context(missing)["whole_table_facts"]["numeric_metrics"]["보험금액"]
    assert money["zero_count"] == 5 and money["missing_or_invalid_count"] == 1
    print("PASS actual zero versus missing value")

    parser = argparse.ArgumentParser()
    parser.add_argument("--source-xlsx", type=Path)
    args = parser.parse_args()
    if args.source_xlsx:
        actual = pd.read_excel(args.source_xlsx)
        assert list(actual.columns) == [
            "순번", "제품코드", "제품명", "규격", "재고위치명", "재고수량", "출고빈도등급",
            "품목손익등급", "품목기여등급", "현보험약가", "보험금액", "표준코드", "제품그룹명",
            "제품구분명", "제품분류명", "발주처코드", "발주처명", "제조사코드",
            "재고위치코드", "KD코드", "EDI코드", "제조사명", "포장단위",
        ]
        assert_full_breakdown(actual)
        print("PASS attached Excel 600 rows: original columns and exact group totals")


if __name__ == "__main__":
    main()

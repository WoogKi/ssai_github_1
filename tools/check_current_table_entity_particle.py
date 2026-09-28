"""In-memory labelled entity filters must preserve particle-like syllables."""
from pathlib import Path
import sys
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from app.ui.current_table_followups.generic import _find_common_column_filter
from tools.check_nlq_casebook_current_table16 import _cases, _dispatch


def main():
    frame = pd.DataFrame({
        "제품명": ["이가탄", "바레탄정", "이가는은정", "가은", "탄"],
        "제조사명": ["이가제약", "가은는제약", "다른제약", "다른제약", "다른제약"],
        "거래처명": ["이가약국", "은는이가약국", "다른약국", "다른약국", "다른약국"],
        "재고수량": [1, 2, 3, 4, 5],
    })
    cases = [
        ("제품명", "이가탄 상세조회", "이가탄"),
        ("제품명", "이가탄 상세히 보여줘", "이가탄"),
        ("제품명", "바레탄정 상세히 보여줘", "바레탄정"),
        ("제품명", "이가탄을 상세조회", "이가탄"),
        ("제품명", "바레탄정은 상세히 보여줘", "바레탄정"),
        ("제품명", "이가는은정 상세조회", "이가는은정"),
        ("제품명", "가은 상세조회", "가은"),
        ("제품명", "이 이가탄 상세조회", "이가탄"),
        ("제품명", ": 이가탄 상세조회", "이가탄"),
        ("거래처명", "이가약국 상세조회", "이가약국"),
        ("거래처명", "은는이가약국을 상세조회", "은는이가약국"),
        ("제조사명", "이가제약 상세조회", "이가제약"),
        ("제조사명", "가은는제약은 상세히 보여줘", "가은는제약"),
    ]
    base = next(c for c in _cases() if c.case_id == "NLQ-0022")
    for column, tail, expected in cases:
        query = f"현재표 {column} {tail}"
        assert _find_common_column_filter(frame, query) == (column, expected), query
        handled, kind, payload = _dispatch(replace(base, frame=frame, query=query, runtime_query=query))
        assert handled and kind == "table", query
        assert payload["extra_meta"]["filter_value"] == expected, query
        assert payload["extra_meta"]["source_call_count"] == 0, query
        wanted = frame.loc[frame[column].str.contains(expected, regex=False)]
        assert payload["df"].equals(wanted), query
        print("PASS", query, "->", expected)
    print(f"SUMMARY {len(cases)}/{len(cases)} PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

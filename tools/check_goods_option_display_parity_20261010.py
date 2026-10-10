"""DB-free NLQ/option product-master display and export parity."""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.sims.nlq.nlq_goods import _build_goods_display_df as nlq_display
from app.sims.views.goods import _build_goods_display_df as option_display
from app.sims.goods_display import order_goods_full_columns
from app.ui.chat_middleware import _make_table_downloads


FRONT = [
    "제품코드", "제품명", "규격", "단위", "제약사명", "제약사 담당자",
    "발주처명", "발주처 담당자", "제품그룹명", "구분명",
]


def main() -> int:
    rows = pd.DataFrame([
        {
            "제품코드": "00024", "제품명": "fixture", "규격": "100T", "단위": "BOX",
            "제약사명": "sample", "제약사 담당자": "maker staff",
            "발주처명": "vendor", "발주처 담당자": "vendor staff",
            "제품그룹명": "group", "구분명": "division",
            "보험수가변경일자": "20261001", "보험가격": 10, "보험단가": 100,
            "보험코드": "code", "이전보험수가변경일자": "20250101",
            "등록일자": "20261001", "extra_raw_column": "retained",
        },
        {
            "제품코드": "00025", "제품명": "fixture2", "규격": "10T", "단위": "EA",
            "제약사명": "sample", "제약사 담당자": "",
            "발주처명": "vendor2", "발주처 담당자": "",
            "제품그룹명": "group", "구분명": "division",
            "보험수가변경일자": "20261001", "보험가격": 20, "보험단가": 200,
            "보험코드": "code2", "이전보험수가변경일자": "20250101",
            "등록일자": "20261001", "extra_raw_column": "retained2",
        },
    ])
    # ERP SELECT order differs from the chat display contract.
    rows = rows.loc[:, ["제품코드", "보험코드", "제품명"]
                    + [column for column in rows if column not in {"제품코드", "보험코드", "제품명"}]]
    for detail in (False, True):
        nlq = nlq_display(rows, detail=detail)
        option = option_display(rows, detail=detail)
        assert nlq.equals(option)
        assert list(option.columns[:10]) == FRONT
        insurance = list(option.columns).index("보험단가")
        assert list(option.columns[insurance - 2:insurance + 3]) == [
            "보험수가변경일자", "보험가격", "보험단가", "보험코드", "이전보험수가변경일자",
        ]
        assert option["제약사 담당자"].tolist() == ["maker staff", ""]
        assert option["발주처 담당자"].tolist() == ["vendor staff", ""]
        assert rows["제품코드"].is_unique and len(option) == len(rows)
        export = order_goods_full_columns(rows, option)
        displayed_raw = [c for c in option if c in rows]
        assert list(export.columns[:len(displayed_raw)]) == displayed_raw
        assert set(export.columns) == set(rows.columns)
        assert export["제품코드"].is_unique
        csv_buf, xlsx_buf = _make_table_downloads(export)
        assert list(pd.read_csv(csv_buf, nrows=0).columns) == list(export.columns)
        assert list(pd.read_excel(xlsx_buf, nrows=0).columns) == list(export.columns)
        assert "extra_raw_column" in export and "extra_raw_column" not in option
    source = (ROOT / "app/sims/views/rddbc_io_goods_views.py").read_text(encoding="utf-8")
    assert "df_display = build_goods_display_df(df, detail=False)" in source
    assert "df = order_goods_full_columns(df, df_display)" in source
    assert '"df": df' in source and '"df_display": df_display' in source
    print("PASS option/NLQ list-detail parity, staff/insurance order, full export columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

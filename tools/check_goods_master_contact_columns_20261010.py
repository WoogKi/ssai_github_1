"""DB-free product-master SIMS manager join and export regression."""

from __future__ import annotations

from pathlib import Path
import re
import sys
import logging
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services import rddbc040_service as service
from app.services.utils import apply_labels
from app.sims.nlq import nlq_goods
from app.ui.chat_middleware import _make_table_downloads
from app.ui.current_table_followups.generic import _find_common_group_column


FRONT = [
    "제품코드", "제품명", "규격", "단위", "제약사명", "제약사 담당자",
    "발주처명", "발주처 담당자", "제품그룹명", "구분명",
]


def main() -> int:
    sql_seen: list[str] = []
    rows = pd.DataFrame([
        {
            "Rd04_Physic_Cd": "00001", "Rd04_Physic_Nm": "첫제품", "Rd04_Standard": "10T",
            "Rd04_Unit": "BOX", "Rd04_Insu_Cd": "8800000000001", "ven_nm": "한미",
            "maker_manager_nm": "김", "order_vendor_nm": "한미", "order_vendor_manager_nm": "김",
            "group_name": "처방", "di_name": "보험", "보험수가변경일자": "20261001",
            "보험가격": 10, "보험단가": 100, "이전보험수가변경일자": "20250101",
            "Rd04_Ven_Cd": "V1", "Rd04_Orven_Cd": "V1", "unused_raw": "keep",
        },
        {
            "Rd04_Physic_Cd": "00002", "Rd04_Physic_Nm": "둘제품", "Rd04_Standard": "20T",
            "Rd04_Unit": "EA", "Rd04_Insu_Cd": "8800000000002", "ven_nm": "동제",
            "maker_manager_nm": "이", "order_vendor_nm": "온라인팜", "order_vendor_manager_nm": "박",
            "group_name": "일반", "di_name": "비보험", "보험수가변경일자": "20261001",
            "보험가격": 20, "보험단가": 200, "이전보험수가변경일자": "20250101",
            "Rd04_Ven_Cd": "V2", "Rd04_Orven_Cd": "O2", "unused_raw": "keep",
        },
        {
            "Rd04_Physic_Cd": "00003", "Rd04_Physic_Nm": "셋제품", "Rd04_Standard": "30T",
            "Rd04_Unit": "EA", "Rd04_Insu_Cd": "8800000000003", "ven_nm": "삼진",
            "maker_manager_nm": "", "order_vendor_nm": "", "order_vendor_manager_nm": "",
            "group_name": "일반", "di_name": "보험", "보험수가변경일자": "20261001",
            "보험가격": 30, "보험단가": 300, "이전보험수가변경일자": "20250101",
            "Rd04_Ven_Cd": "V3", "Rd04_Orven_Cd": None, "unused_raw": "keep",
        },
    ])

    def fake_read_df(sql: str, params: object = None) -> pd.DataFrame:
        sql_seen.append(sql)
        if "COUNT(1)" in sql:
            return pd.DataFrame({"cnt": [3]})
        return rows.head(1).copy() if re.search(r"\bTOP 1\b", sql) else rows.copy()

    original_read = service.read_df
    original_log = service._log_sql
    service.read_df = fake_read_df
    service._log_sql = lambda *_args, **_kwargs: None
    try:
        listing = service.search_goods_full.__wrapped__(
            top=10, ven_nm_kw="동제", with_count=True,
        )
        detail = service.get_goods_detail_full.__wrapped__(physic_cd="00001")
    finally:
        service.read_df = original_read
        service._log_sql = original_log

    assert len(listing) == 3 and listing["Rd04_Physic_Cd"].is_unique
    assert len(detail) == 1 and detail.iloc[0]["Rd04_Physic_Cd"] == "00001"
    for sql in sql_seen:
        assert "LEFT JOIN dbo.Rddbc030 v" in sql and "LEFT JOIN dbo.Rddbc030 ov" in sql
        assert "LEFT JOIN dbo.Rddbc060 ms" in sql and "LEFT JOIN dbo.Rddbc060 os" in sql
        assert "a.Rd04_Ven_Cd = v.Rd03_Ven_Cd" in sql
        assert "a.Rd04_Orven_Cd = ov.Rd03_Ven_Cd" in sql
        assert "v.Rd03_Sales_Man = ms.Rd06_User_Cd" in sql
        assert "ov.Rd03_Sales_Man = os.Rd06_User_Cd" in sql
        if "COUNT(1)" not in sql:
            assert "ms.Rd06_User_Nm" in sql and "os.Rd06_User_Nm" in sql
        assert "Rd03_Take_Nm" not in sql
    assert sum("LEFT JOIN dbo.Rddbc030" in sql for sql in sql_seen) == 3

    labeled = apply_labels(listing, "rddbc040")
    assert labeled["제약사 담당자"].tolist() == ["김", "이", ""]
    assert labeled["발주처명"].tolist() == ["한미", "온라인팜", ""]
    assert labeled["발주처 담당자"].tolist() == ["김", "박", ""]
    for detail_mode in (False, True):
        display = nlq_goods._build_goods_display_df(labeled, detail=detail_mode)
        assert list(display.columns[:10]) == FRONT
        insurance = list(display.columns).index("보험단가")
        assert list(display.columns[insurance - 2:insurance + 3]) == [
            "보험수가변경일자", "보험가격", "보험단가", "보험코드", "이전보험수가변경일자",
        ]
        assert display["보험코드"].tolist() == labeled["보험코드"].tolist()

    captured: list[dict] = []
    original_push = nlq_goods.push_sims_result_to_chat
    nlq_goods.push_sims_result_to_chat = lambda result, action: captured.append(result)
    try:
        for detail_mode in (False, True):
            display = nlq_goods._build_goods_display_df(labeled, detail=detail_mode)
            nlq_goods._push_goods_result(
                txt="제품코드 조회", title="제품코드 상세" if detail_mode else "제품코드 목록",
                action="제품코드 상세" if detail_mode else "제품코드 목록",
                df=labeled, df_display=display, params_out={"TopN": 10},
                query_summary="제품코드 조회", source="제품코드마스터",
            )
    finally:
        nlq_goods.push_sims_result_to_chat = original_push
    assert len(captured) == 2
    for result in captured:
        assert list(result["df_display"].columns[:10]) == FRONT
        assert list(result["df"].columns[:10]) == FRONT
        assert len(result["df"]) == len(result["df_display"]) == 3
        assert set(result["df"].columns) == set(labeled.columns)
        assert "unused_raw" in result["df"].columns and "unused_raw" not in result["df_display"].columns
        assert result["meta"]["row_count_total"] == 3
        assert _find_common_group_column(
            result["df"], "현재표 발주처 담당자별 집계",
            result["action"],
        ) == "발주처 담당자"
        csv_buf, xlsx_buf = _make_table_downloads(result["df"])
        assert list(pd.read_csv(csv_buf, nrows=0).columns[:10]) == FRONT
        assert list(pd.read_excel(xlsx_buf, nrows=0).columns[:10]) == FRONT

    route_calls: list[tuple[str, dict]] = []
    with (
        patch.object(nlq_goods, "get_goods_detail_full", side_effect=lambda **kwargs: route_calls.append(("detail", kwargs)) or rows.head(1).copy()),
        patch.object(nlq_goods, "search_goods_full", side_effect=lambda **kwargs: route_calls.append(("list", kwargs)) or rows.copy()),
        patch.object(nlq_goods, "push_sims_result_to_chat", return_value=None),
    ):
        for question in ("제품코드 00001 조회", "제약사명 동제 제품 조회"):
            assert nlq_goods.try_handle_goods_nlq(
                question, room={}, session_state={}, make_ts=lambda: "", next_seq=lambda: 1,
                logger=logging.getLogger(__name__),
            )
    assert route_calls[0] == ("detail", {"physic_cd": "00001"}), route_calls
    assert route_calls[1][0] == "list" and route_calls[1][1]["ven_nm_kw"] == "동제", route_calls
    print("PASS: independent R030/R060 manager joins, three products, list/detail/display/export order, full-column preservation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

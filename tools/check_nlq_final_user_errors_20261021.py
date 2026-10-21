"""DB-free regressions for the 2026-10-21 user NLQ failures."""

from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.io_nlq import resolve_io_nlq
from app.sims.nlq import nlq_router
from app.ui.chat_middleware import _build_sims_sales_group_profile, _build_sims_sales_time_profile
from app.ui.current_table_followups.action_dispatcher import handle_current_table_followup_by_action
from app.ui.current_table_followups.generic import _explicit_common_filter_conditions


def _followup(df: pd.DataFrame, question: str) -> tuple[str, dict]:
    pushed: list[tuple[str, dict]] = []
    handled = handle_current_table_followup_by_action(
        df=df, query=question, top_n=300, table_key="company8-original",
        source_action="발주조회",
        helpers={
            "to_num": lambda values: pd.to_numeric(values, errors="coerce").fillna(0),
            "push_table": lambda **kwargs: pushed.append(("table", kwargs)) or True,
            "push_notice": lambda **kwargs: pushed.append(("notice", kwargs)) or True,
        },
        log=logging.getLogger(__name__),
        source_meta={"result_status": "success", "row_count_total": len(df)},
    )
    assert handled and len(pushed) == 1, (question, handled, pushed)
    return pushed[0]


def check_order_parser_and_router() -> None:
    today = date(2026, 10, 20)
    question = "어제 발주 조회 등록자 오준혁"
    parsed = resolve_io_nlq(question, today=today)
    assert parsed and parsed["action"] == "발주조회"
    assert parsed["params"]["_unsupported_order_conditions"] == ["등록자 오준혁"]
    assert parsed["params"]["date_from"] == "20261019"
    delivered: list[dict] = []
    with (
        patch("app.ui.chat_middleware.push_sims_result_to_chat", side_effect=lambda payload, _: delivered.append(payload)),
        patch("app.db.mssql_client.get_conn", side_effect=AssertionError("DB called")) as db,
        patch("app.services.rddbc170_rddbc180_order_service.get_order_result", side_effect=AssertionError("ERP called")) as service,
    ):
        handled = nlq_router._try_handle_io_nlq(
            question, room={"messages": []}, session_state={}, make_ts=lambda: "fixture",
            next_seq=lambda: 1, logger=logging.getLogger(__name__),
        )
    assert handled and len(delivered) == 1
    notice = delivered[0]
    assert notice["meta"]["result_status"] == "input_required"
    assert notice["meta"]["source_call_count"] == 0
    assert "등록자 오준혁" in notice["message"] and "발주담당자" not in notice["message"]
    assert db.call_count == service.call_count == 0

    for name in ("윤상민", "이준휘"):
        for label in ("발주담당자", "발주담당자명"):
            result = resolve_io_nlq(f"어제 발주 조회 {label} {name}", today=today)
            assert result and result["params"].get("order_staff_nm") == name, (label, name, result)
    pharma = resolve_io_nlq("어제 발주 조회 제약담당자명 윤상민", today=today)
    assert pharma and pharma["params"].get("pharma_staff_nm") == "윤상민"
    product = resolve_io_nlq("제품정보 등록자 오준혁", today=today)
    assert product and product["params"].get("add_nm") == "오준혁", product
    assert resolve_io_nlq("어제 발주 조회", today=today)["action"] == "발주조회"
    assert resolve_io_nlq("발주계산", today=today)["action"] == "발주 계산"


def check_current_table() -> None:
    source = pd.DataFrame({
        "재고위치명": ["남양주지점", "남양주지점", "본사 창고"],
        "발주담당자명": ["윤상민", "이준휘", "윤상민"],
        "제품명": ["조플루자정", "아스피린", "제품 재고위치명"],
        "발주수량": [3, 4, 5],
    })
    original = source.copy(deep=True)
    kind, payload = _followup(source, "현재표 재고위치명 남양주지점 발주담당자명 윤상민")
    assert kind == "notice", payload
    assert payload["extra_meta"]["result_status"] == "input_required"
    assert payload["extra_meta"]["source_call_count"] == 0
    assert payload["extra_meta"]["unsupported_conditions"] == [
        "재고위치명 남양주지점", "발주담당자명 윤상민",
    ]
    assert "재고위치명 남양주지점" in payload["message"]
    assert "발주담당자명 윤상민" in payload["message"]
    pd.testing.assert_frame_equal(source, original)
    for question, expected in (
        ("현재표 재고위치명 남양주지점", 2),
        ("현재표 발주담당자명 윤상민", 2),
        ("현재표 발주담당자명 이준휘", 1),
        ("현재표 제품명 조플루자정", 1),
    ):
        kind, payload = _followup(source, question)
        assert kind == "table" and len(payload["df"]) == expected, (question, kind, payload)
    pd.testing.assert_frame_equal(source, original)
    assert _explicit_common_filter_conditions(source, "현재표 제품명 재고위치명정") == [
        "제품명 재고위치명정",
    ]


def check_amount() -> None:
    sales = pd.DataFrame({
        "매출처수": [2, 3], "총매출액": [100, 200], "발주수량": [1, 2],
        "거래일자": ["20261019", "20261020"],
    })
    assert _build_sims_sales_time_profile(sales)["amount_col"] == "총매출액"
    assert _build_sims_sales_group_profile(sales)["amount_col"] == "총매출액"
    for column in ("매출합계", "매출공급가액", "합계금액"):
        frame = pd.DataFrame({"거래일자": ["20261019"], "매출처수": [7], column: [1200]})
        assert _build_sims_sales_time_profile(frame)["amount_col"] == column
        assert _build_sims_sales_group_profile(frame)["amount_col"] == column
    no_amount = pd.DataFrame({
        "발주일자": ["20261019"], "예상매출처명": ["A"],
        "거래처수": [2], "제품수": [3], "집계건수": [4], "발주수량": [5],
    })
    assert _build_sims_sales_time_profile(no_amount)["amount_col"] is None
    group = _build_sims_sales_group_profile(no_amount)
    assert group["amount_col"] is None and not group["vendor_sales_top"]


def main() -> None:
    check_order_parser_and_router()
    check_current_table()
    check_amount()
    print("PASS NLQ final user errors 20261021: router, current-table, names, amounts; DB calls=0")


if __name__ == "__main__":
    main()

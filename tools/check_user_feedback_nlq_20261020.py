"""DB-free regressions for the 2026-10-20 company-8 user questions."""

from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.erp_table_nlq import is_order_calculation_request
from app.services.io_nlq import resolve_io_nlq, resolve_stock_location_codes
from app.sims.meta.erp_table_feature_registry import match_action_in_text
from app.sims.nlq.nlq_router import _extract_analytics_stock_location_terms
from app.ui.chat_middleware import _build_sims_sales_group_profile, _build_sims_sales_time_profile
from app.ui.current_table_followups.action_dispatcher import handle_current_table_followup_by_action
from app.ui.current_table_followups.order import handle_order_followup


def _followup(df: pd.DataFrame, question: str) -> dict:
    pushed: list[tuple[str, dict]] = []
    handled = handle_current_table_followup_by_action(
        df=df, query=question, top_n=20, table_key="original-2310",
        source_action="발주조회",
        helpers={
            "to_num": lambda values: pd.to_numeric(values, errors="coerce").fillna(0),
            "push_table": lambda **kwargs: (pushed.append(("table", kwargs)) or True),
            "push_notice": lambda **kwargs: (pushed.append(("notice", kwargs)) or True),
        },
        log=logging.getLogger(__name__),
        source_meta={"result_status": "success", "row_count_total": len(df)},
    )
    assert handled and len(pushed) == 1 and pushed[0][0] == "table", (question, pushed)
    return pushed[0][1]


def _order_source() -> pd.DataFrame:
    rows = 2310
    df = pd.DataFrame({
        "발주일자": ["20261019"] * rows,
        "발주거래처코드": ["V01"] * 1000 + ["V02"] * (rows - 1000),
        "발주거래처명": ["공급처 A"] * 1000 + ["공급처 B"] * (rows - 1000),
        "발주수량": [2] * rows,
        "입고수량": [1] * rows,
        "미입고수량": [1] * rows,
        "예상매출처명": ["매출처 A"] * rows,
    })
    while len(df.columns) < 29:
        df[f"원본필드{len(df.columns)}"] = ""
    return df


def check_current_table() -> None:
    original = _order_source()
    monthly_pushed: list[dict] = []
    assert handle_order_followup(
        df=original, query="현재표 발주일자 기준 월별 발주 집계", top_n=20,
        table_key="original-2310", source_action="발주조회",
        helpers={
            "_is_deterministic_request": True,
            "_requested_grouping": "month",
            "_resolved_date_column": "발주일자",
            "to_num": lambda values: pd.to_numeric(values, errors="coerce").fillna(0),
            "push_table": lambda **kwargs: (monthly_pushed.append(kwargs) or True),
            "push_notice": lambda **kwargs: False,
        },
        log=logging.getLogger(__name__),
    )
    assert len(monthly_pushed) == 1
    monthly = monthly_pushed[0]
    vendor = _followup(original, "현재표 발주거래처명 집계")
    old_alias = _followup(original, "현재표 발주처명 집계")
    month_df, vendor_df = monthly["df"], vendor["df"]
    assert len(original) == 2310 and len(original.columns) == 29
    assert len(month_df) == 1 and int(month_df["건수"].sum()) == 2310
    assert len(vendor_df) == 2 and int(vendor_df["행수"].sum()) == 2310
    assert vendor["source_table_key"] == "original-2310" and vendor["source_rows"] == 2310
    assert old_alias["df"]["행수"].sum() == 2310
    for column in ("발주수량", "입고수량", "미입고수량"):
        assert int(vendor_df[column].sum()) == int(original[column].sum())
    assert not any("금액" in str(column) for column in vendor_df.columns)
    assert "발주거래처명" in original.columns


def check_order_route() -> None:
    today = date(2026, 10, 20)
    yesterday = resolve_io_nlq("어제 발주 조회", today=today)
    assert yesterday and yesterday["action"] == "발주조회"
    assert yesterday["params"].get("date_from") == "20261019"
    assert yesterday["params"].get("date_to") == "20261019"
    for question in ("제약사 베링거 발주", "제약사 베링거 발주 조회"):
        result = resolve_io_nlq(question, today=today)
        assert result and result["action"] == "발주조회", (question, result)
        assert result["params"].get("maker_nm") == "베링거", (question, result)
    assert resolve_io_nlq("발주계산", today=today)["action"] == "발주 계산"
    assert is_order_calculation_request("발주계산")
    assert match_action_in_text("발주처 베링거") is None
    assert match_action_in_text("발주담당 신") is None
    assert match_action_in_text("제약사 베링거 제품코드 목록") is None


def check_locations() -> None:
    locations = {
        "00001": "본사 창고", "00008": "안양지점", "00009": "안양임시매입",
        "00012": "안양반품조정", "00019": "안양지점할당창고",
    }
    cases = (
        ("현재고 재고위치 안양", [], "location_name_ambiguous"),
        ("현재고 재고위치 안양지점", ["00008"], ""),
        ("현재고 재고위치 (안양지점)", ["00008"], ""),
        ("현재고 재고위치 00008", ["00008"], ""),
        ("현재고 재고위치 없는위치", [], "location_name_unresolved"),
    )
    for question, expected_codes, expected_error in cases:
        terms = _extract_analytics_stock_location_terms(question)
        assert terms is not None
        selected, error, _ = resolve_stock_location_codes(
            *terms, allowed_codes=list(locations), location_names=locations,
        )
        assert (selected, error) == (expected_codes, expected_error), (question, selected, error)
    selected, error, _ = resolve_stock_location_codes(
        ["00008"], ["(안양지점)"], allowed_codes=list(locations), location_names=locations,
    )
    assert selected == ["00008"] and not error
    selected, error, _ = resolve_stock_location_codes(
        [], ["(본사 창고)"], allowed_codes=list(locations), location_names=locations,
    )
    assert selected == ["00001"] and not error
    selected, error, _ = resolve_stock_location_codes(
        [], ["(안양)(지점)"], allowed_codes=list(locations), location_names=locations,
    )
    assert not selected and error == "location_name_unresolved"
    jeonju = {"00247": "(전주)창고", "00902": ".전주_반품대기"}
    selected, error, _ = resolve_stock_location_codes(
        [], ["(전주)"], allowed_codes=list(jeonju), location_names=jeonju,
    )
    assert selected == ["00247"] and not error


def check_amount() -> None:
    order = _order_source().head(3)
    time_profile = _build_sims_sales_time_profile(order)
    group_profile = _build_sims_sales_group_profile(order)
    assert time_profile["available"] is False and time_profile["amount_col"] is None
    assert group_profile["amount_col"] is None
    assert group_profile["product_sales_top"] == [] and group_profile["vendor_sales_top"] == []
    assert sum(row["수량"] for row in group_profile["vendor_quantity_top"]) == 3
    assert _build_sims_sales_time_profile(order, {"amount_priority": ("발주금액",)})["amount_col"] is None
    for action_column in ("매출금액", "입고금액", "출고금액"):
        frame = pd.DataFrame({"거래일자": ["20261019", "20261020"], action_column: ["1,200원", "300원"], "수량": [1, 2]})
        result = _build_sims_sales_time_profile(frame)
        assert result["available"] and result["amount_col"] == action_column
        assert sum(row["금액"] for row in result["monthly_sales"]) == 1500


def main() -> None:
    check_current_table()
    check_order_route()
    check_locations()
    check_amount()
    print("PASS user feedback NLQ 20261020: source=2310/29, order routing, locations, amount eligibility; DB calls=0")


if __name__ == "__main__":
    main()

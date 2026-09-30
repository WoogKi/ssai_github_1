"""Offline regression checks for user-log routing and text composer dispatch."""

from datetime import date
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.erp_table_nlq import is_order_calculation_request, resolve_registered_erp_table_nlq
from app.services.order_calculation_contract import OrderConditions, closing_day_relation
from app.sims.nlq.nlq_router import (
    _build_analytics_params,
    _resolve_analytics_action,
    resolve_new_sims_nlq_candidate,
)


def _assert(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def test_shortage_aliases_keep_manufacturer_scope() -> None:
    cases = (
        ("매입처별 부족예상 제약사 사노피", "매입처별 재고부족 현황"),
        ("제약사 사노피 부족품목", "품목별 재고부족현황"),
    )
    for question, expected_action in cases:
        action = _resolve_analytics_action(question)
        params = _build_analytics_params(question, action or "")
        _assert(action == expected_action, f"shortage route mismatch: {question!r} -> {action!r}")
        _assert(params.get("maker_nm") == "사노피", f"maker scope lost: {question!r} -> {params!r}")
        _assert(not params.get("physic_nm"), f"product-master residual leaked: {question!r} -> {params!r}")


def test_order_calculation_natural_phrases() -> None:
    for question in (
        "제약사 피엠지 발주 예상품목",
        "제약사 피엠지 발주 예상품목 알려줘",
        "제약사 피엠지 발주수량 뽑아줘",
        "제약사 피엠지 발주수량 조회조건 전체 뽑아줘",
    ):
        resolved = resolve_registered_erp_table_nlq(question, today=date(2026, 9, 30))
        _assert(is_order_calculation_request(question), f"calculation intent missed: {question!r}")
        _assert((resolved or {}).get("action") == "발주 계산", f"calculation action mismatch: {resolved!r}")
        params = (resolved or {}).get("params") or {}
        _assert(params.get("maker_nm") == "피엠지", f"maker scope lost: {question!r} -> {params!r}")
        _assert(not params.get("physic_nm"), f"product-master residual leaked: {question!r} -> {params!r}")
        _assert(
            (resolve_new_sims_nlq_candidate(question) or {}).get("action") == "발주 계산",
            f"router precedence mismatch: {question!r}",
        )
        if "조회조건 전체" in question:
            _assert(params.get("query_mode") == "전체", f"query mode lost: {params!r}")


def test_closing_day_month_end_boundary() -> None:
    september = OrderConditions(date(2026, 9, 25), closing_day=31)
    february = OrderConditions(date(2026, 2, 20), closing_day=30)
    leap_february = OrderConditions(date(2024, 2, 20), closing_day=31)
    _assert(september.effective_closing_day == 30, "September 31 must apply September month-end")
    _assert(february.effective_closing_day == 28, "February 30 must apply February month-end")
    _assert(leap_february.effective_closing_day == 29, "Leap February must retain leap month-end")
    _assert(closing_day_relation(september) == "BEFORE", "effective settlement boundary not used")


def test_text_composer_callback_dedup_contract() -> None:
    source = (Path(__file__).resolve().parents[1] / "app" / "Lmstudio_SSAI_chat_main.py").read_text(encoding="utf-8")
    _assert("def _queue_chat_composer_submission" in source, "text composer callback missing")
    _assert('st.session_state["__chat_composer_callback_text"] = composer_text' in source, "callback dispatch marker missing")
    _assert("if composer_text != callback_text:" in source, "composer callback duplicate guard missing")
    _assert('st.session_state["__sims_auto_user_input"] = composer_text' in source, "shared text dispatch queue missing")
    _assert("chat_history_container = st.container()" in source, "stable history container missing")
    _assert("chat_immediate_user_slot = st.empty()" in source, "immediate user slot missing")
    _assert("chat_pending_area_slot = st.empty()" in source, "pending answer slot missing")
    _assert("pending_area = chat_pending_area_slot.container()" in source, "pending answer order missing")
    _assert(
        source.index("chat_immediate_user_slot = st.empty()") < source.index("chat_pending_area_slot = st.empty()"),
        "user bubble must precede the current-turn answer slot",
    )
    _assert('with st.chat_message("user"):' in source, "immediate user chat bubble missing")
    _assert("immediate_echo_message_id = user_message[\"id\"]" in source, "same-run echo identity missing")
    _assert(
        "if immediate_echo_message_id and str(m.get(\"id\") or \"\") == immediate_echo_message_id:" in source,
        "same-run history duplicate guard missing",
    )


def main() -> None:
    test_shortage_aliases_keep_manufacturer_scope()
    test_order_calculation_natural_phrases()
    test_closing_day_month_end_boundary()
    test_text_composer_callback_dedup_contract()
    print("PASS: user-log routing and chat echo contract")


if __name__ == "__main__":
    main()

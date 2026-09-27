"""Focused no-DB contract gate for real SIMS NLQ action-to-handler parameters."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from app.services.erp_table_nlq import resolve_registered_erp_table_nlq
from app.services.io_nlq import extract_nlq_natural_period, resolve_io_nlq
from app.sims.nlq import nlq_router


FIXED_NOW = datetime(2026, 9, 27, 12, 0, 0)
LOG = logging.getLogger("check_nlq_runtime_intent_contract")


def _payload(params: dict[str, Any]) -> dict[str, Any]:
    frame = pd.DataFrame([{"fixture": "ok"}])
    return {
        "final": True,
        "type": "table",
        "df": frame,
        "df_display": frame,
        "records": frame.to_dict(orient="records"),
        "params": dict(params),
        "meta": {"row_count": 1, "row_count_total": 1, "result_status": "success"},
    }


def _run_router(question: str) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []

    def service(params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        received = dict(params or kwargs.get("params") or {})
        calls.append(received)
        return _payload(received)

    def push(payload: dict[str, Any], action: str) -> dict[str, Any]:
        sent.append({"action": action, "payload": payload})
        return dict(payload.get("meta") or {})

    def current_stock_entity(_text: str, *, params: dict[str, Any] | None = None, **_kwargs: Any) -> dict[str, Any]:
        resolved = dict(params or {})
        resolved.update({"physic_cd": "00301", "physic_nm": "이가탄"})
        return {"status": "resolved", "params": resolved, "resolved_kind": "product"}

    with (
        patch("app.services.datetime_tool.operating_now", return_value=FIXED_NOW),
        patch("app.services.rddbc110_service.get_rddbc110_result", service),
        patch("app.services.rddbc120_service.get_rddbc120_result", service),
        patch("app.services.product_inventory_service.get_product_inventory_result", service),
        patch("app.services.rddbc170_rddbc180_order_service.get_order_result", service),
        patch("app.services.io_nlq.resolve_current_stock_entity_condition", current_stock_entity),
        patch("app.sims.views.rddbc_io_shared._load_stock_code_options", return_value=[]),
        patch("app.ui.chat_middleware.push_sims_result_to_chat", push),
    ):
        handled = nlq_router._try_handle_io_nlq(
            question,
            room={},
            session_state={},
            make_ts=lambda: "2026-09-27T12:00:00",
            next_seq=lambda: 1,
            logger=LOG,
        )
    return handled, calls, sent


def _assert(name: str, condition: bool, detail: object) -> bool:
    marker = "PASS" if condition else "FAIL"
    print(f"[{marker}] {name}: {detail}")
    return condition


def _handler_case(name: str, question: str, action: str, expected: dict[str, Any]) -> bool:
    parsed = resolve_io_nlq(question)
    handled, calls, sent = _run_router(question)
    received = calls[0] if len(calls) == 1 else {}
    matches = all(received.get(key) == value for key, value in expected.items())
    ok = (
        isinstance(parsed, dict)
        and parsed.get("action") == action
        and handled
        and len(calls) == 1
        and len(sent) == 1
        and sent[0].get("action") == action
        and matches
    )
    return _assert(name, ok, {"parsed": parsed, "handler": received, "sent": len(sent)})


def main() -> int:
    checks: list[bool] = []
    checks.append(_handler_case("이가탄 재고", "이가탄 재고", "현재고 조회", {"physic_nm": "이가탄", "physic_cd": "00301"}))
    checks.append(_handler_case("이가탄 재고현황", "이가탄 재고현황", "현재고 조회", {"physic_nm": "이가탄", "physic_cd": "00301"}))
    checks.append(_handler_case("이가탄 재고수량", "이가탄 재고수량", "현재고 조회", {"physic_nm": "이가탄", "physic_cd": "00301"}))
    checks.append(_handler_case("이가탄 재고 현황 조회", "이가탄 재고 현황 조회", "현재고 조회", {"physic_nm": "이가탄", "physic_cd": "00301"}))
    checks.append(_handler_case("이가탄 재고장", "이가탄 재고장", "제품재고현황 조회", {"nlq_unlabeled_name": "이가탄"}))
    checks.append(_handler_case("이가탄 제품재고장", "이가탄 제품재고장", "제품재고현황 조회", {"nlq_unlabeled_name": "이가탄"}))
    checks.append(_handler_case("이가탄 제품재고현황", "이가탄 제품재고현황", "제품재고현황 조회", {"nlq_unlabeled_name": "이가탄"}))
    checks.append(_handler_case("이가탄 제품 재고 현황", "이가탄 제품 재고 현황", "제품재고현황 조회", {"nlq_unlabeled_name": "이가탄"}))
    checks.append(_handler_case("이가탄 매출", "이가탄 매출", "출고명세 조회", {"nlq_unlabeled_name": "이가탄"}))
    checks.append(_handler_case("이가탄 매출 202609", "이가탄 매출 202609", "출고명세 조회", {"nlq_unlabeled_name": "이가탄", "date_from": "20260901", "date_to": "20260930"}))
    checks.append(_handler_case("메트로 매출", "메트로 매출", "출고명세 조회", {"nlq_unlabeled_name": "메트로"}))
    checks.append(_handler_case("메트로 매출 202609", "메트로 매출 202609", "출고명세 조회", {"nlq_unlabeled_name": "메트로", "date_from": "20260901", "date_to": "20260930"}))
    checks.append(_handler_case("지난주 입고현황", "지난주 입고현황 조회", "입고명세 조회", {"date_from": "20260914", "date_to": "20260920"}))
    checks.append(_handler_case("발주담당자 김 발주 조회", "발주담당자 김 발주 조회", "발주조회", {"order_staff_nm": "김"}))
    checks.append(_handler_case("발주담당자 김 발주 조회 202609", "발주담당자 김 발주 조회 202609", "발주조회", {"order_staff_nm": "김", "month_from": "202609", "month_to": "202609"}))
    checks.append(_handler_case("제약담당자 이 발주 조회 202609", "제약담당자 이 발주 조회 202609", "발주조회", {"pharma_staff_nm": "이", "month_from": "202609", "month_to": "202609"}))

    last_week = extract_nlq_natural_period("지난주 입고현황 조회", today=FIXED_NOW.date())
    checks.append(_assert("지난주 공통 기간 authority", last_week == {"date_from": "20260914", "date_to": "20260920", "_nlq_period_kind": "previous_calendar_week"}, last_week))
    period_expectations = {
        "오늘": ("20260927", "20260927"),
        "어제": ("20260926", "20260926"),
        "이번주": ("20260921", "20260927"),
        "지난주": ("20260914", "20260920"),
        "이번달": ("202609", "202609"),
        "지난달": ("202608", "202608"),
        "202609": ("202609", "202609"),
    }
    for token, expected in period_expectations.items():
        period = extract_nlq_natural_period(token, today=FIXED_NOW.date())
        actual = (period.get("date_from") or period.get("month_from"), period.get("date_to") or period.get("month_to"))
        checks.append(_assert(f"공통 기간 {token}", actual == expected, period))
    inbound_handled, inbound_calls, _ = _run_router("지난주 입고현황 조회")
    inbound_params = inbound_calls[0] if inbound_handled and len(inbound_calls) == 1 else {}
    checks.append(_assert("지난주 entity 미소비", not inbound_params.get("nlq_unlabeled_name"), inbound_params))
    checks.append(_assert("재고 부족 설명 bypass", all(nlq_router.resolve_new_sims_nlq_candidate(question) is None for question in ("재고 부족 기준이 뭐야?", "재고 부족 산정 기준 알려줘")), "candidate=None"))

    registered = resolve_registered_erp_table_nlq("발주담당자 김 발주 조회 202609", today=FIXED_NOW.date()) or {}
    checks.append(_assert("labelled 기간 경계 parser", (registered.get("params") or {}).get("order_staff_nm") == "김", registered))
    print(f"RESULT: {'PASS' if all(checks) else 'FAIL'} ({sum(checks)}/{len(checks)})")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())

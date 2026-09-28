"""Focused no-DB runtime contract for transaction-statement direction syntax."""

from __future__ import annotations

from datetime import date, datetime
import inspect
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services import io_nlq
from app.services import rddbc130_service
from app.sims.nlq import nlq_router


REFERENCE_DAY = date(2026, 9, 27)
FIXED_NOW = datetime(2026, 9, 27, 12, 0, 0)
LOG = logging.getLogger("check_transaction_statement_common_direction")

CASES = (
    ("출고 거래명세서 공통 조회 202609", "3", ""),
    ("매출 거래명세서 공통 조회 202609", "3", ""),
    ("거래명세서 공통 출고 조회 202609", "3", ""),
    ("거래명세서 공통 매출 조회 202609", "3", ""),
    ("입고 거래명세서 공통 조회 202609", "1", ""),
    ("매입 거래명세서 공통 조회 202609", "1", ""),
    ("거래명세서 공통 입고 조회 202609", "1", ""),
    ("거래명세서 공통 매입 조회 202609", "1", ""),
    ("거래명세서 공통 조회 202609", "", ""),
    ("출고 거래명세서 공통 거래처명 대학약국 조회 202609", "3", "대학약국"),
    ("매입 거래명세서 공통 거래처명 인천약품 조회 202609", "1", "인천약품"),
)


def _service_payload(params: dict[str, Any]) -> dict[str, Any]:
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
        return _service_payload(received)

    def push(payload: dict[str, Any], action: str) -> dict[str, Any]:
        sent.append({"action": action, "payload": payload})
        return dict(payload.get("meta") or {})

    with (
        patch("app.services.datetime_tool.operating_now", return_value=FIXED_NOW),
        patch("app.services.rddbc130_service.get_rddbc130_result", service),
        patch.object(nlq_router, "_get_trans_doc_full_summary", return_value={}),
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


def _check_case(query: str, expected_direction: str, expected_vendor: str) -> list[str]:
    errors: list[str] = []
    parsed = io_nlq.resolve_io_nlq(query, today=REFERENCE_DAY) or {}
    action = str(parsed.get("action") or "")
    params = dict(parsed.get("params") or {})
    consumed = io_nlq._consume_io_action_text(query, action)
    residual = io_nlq._extract_unlabeled_entity_phrase(query, action)
    resolved = io_nlq.resolve_unlabeled_io_entity_condition(
        query,
        action=action,
        params=params,
        residual_phrase=residual,
    )
    final_params, policy = io_nlq.apply_nlq_default_period_policy(
        dict(resolved.get("params") or {}),
        action,
        today=REFERENCE_DAY,
    )
    handled, calls, sent = _run_router(query)
    service_params = calls[0] if len(calls) == 1 else {}
    sent_meta = dict((sent[0].get("payload") or {}).get("meta") or {}) if sent else {}

    expected = {
        "month_from": "202609",
        "month_to": "202609",
        "date_from": "20260901",
        "date_to": "20260930",
    }
    if expected_direction:
        expected["trans_di"] = expected_direction
    if expected_vendor:
        expected["ven_nm"] = expected_vendor

    if action != "거래명세서 공통 조회":
        errors.append(f"action={action!r}")
    if residual:
        errors.append(f"residual={residual!r}, consumed={consumed!r}")
    if resolved.get("status") != "not_applicable":
        errors.append(f"resolver_status={resolved.get('status')!r}")
    if any(final_params.get(key) != value for key, value in expected.items()):
        errors.append(f"final_params={final_params!r}")
    if policy.get("auto_applied"):
        errors.append(f"period_policy={policy!r}")
    if not handled or len(calls) != 1 or len(sent) != 1:
        errors.append(f"handled={handled!r}, calls={len(calls)}, sent={len(sent)}")
    if any(service_params.get(key) != value for key, value in expected.items()):
        errors.append(f"service_params={service_params!r}")
    if sent_meta.get("service_call_skipped") is True:
        errors.append(f"service_call_skipped={sent_meta!r}")
    return errors


def main() -> int:
    failures: list[tuple[str, list[str]]] = []
    for query, direction, vendor in CASES:
        errors = _check_case(query, direction, vendor)
        if errors:
            failures.append((query, errors))
            print(f"[FAIL] {query}: {'; '.join(errors)}")
        else:
            print(f"[PASS] {query}")

    service_source = inspect.getsource(rddbc130_service.get_rddbc130_result)
    source_contract_ok = service_source.count("get_rddbc130_df(params)") == 1
    print(f"[{'PASS' if source_contract_ok else 'FAIL'}] rddbc130 display service keeps one query entrypoint")
    if not source_contract_ok:
        failures.append(("source_call_count", ["rddbc130 display query entrypoint changed"]))

    total = len(CASES) + 1
    print(f"RESULT: {'PASS' if not failures else 'FAIL'} ({total - len(failures)}/{total})")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

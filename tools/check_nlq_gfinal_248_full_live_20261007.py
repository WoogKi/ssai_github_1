"""One-pass, company-3-only GFINAL-248 production-faithful live runner.

The preflight mode makes no ERP call.  Live mode is explicit and has no retry
path: each Golden case is persisted before and after its one invocation.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.mssql_client import get_current_company_id, set_current_company_id
from tools.check_nlq_golden_250_casebook import audit
from tools.check_nlq_golden_250_focused_smoke_20261007 import (
    DeliveryCapture,
    _RunPersistence,
    _delivery_audit,
    _load,
    _payload_action,
    _payload_meta,
    _payload_rows,
    _write,
)
from tools.check_nlq_official_206_regression import _action_name_matches
from tools.check_nlq_regression_harness_alignment_20261006 import FocusedCase, _run_current_table


EXPECTED_CASEBOOK_CHECKSUM = "55682fdb6db63d64b5e4787cb0d437faf20c122034f07a0197e1689ed9d05378"
SHORTAGE_PARENT = "매입처별 재고부족현황"
SHORTAGE_QUESTIONS = {
    "현재표 부족제품수 top 10 보여줘",
    "현재표 관련제품수 top 10",
    "현재표 재고없음제품수 top 10",
    "현재표 음수재고제품수 top 10",
    "현재표 매입처원본재고금액 top 10",
}
LOG = logging.getLogger("gfinal-248-full-live")


def _casebook_checksum(cases: list[dict[str, Any]]) -> str:
    value = json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_state(case_id: str) -> dict[str, Any]:
    return {
        "current_room": {"id": f"gfinal-full-{case_id}", "company": "3", "messages": []},
        "chat_rooms": [], "sims_tables": {}, "sims_export_tables": {},
        "__sims_export_tables_by_key": {}, "__io_pending_product_pick": {},
    }


def _assert_sibling_restore_contract() -> None:
    """Prove the cache used by ``run`` cannot feed sibling output onward."""
    parent_calls = 0
    original = {"stash": {"source": "parent", "rows": [1, 2, 3]}}
    cache: dict[tuple[int, str], dict[str, Any]] = {}
    key = (3, SHORTAGE_PARENT)
    for question in sorted(SHORTAGE_QUESTIONS):
        cached = cache.get(key)
        if cached is None:
            parent_calls += 1
            cache[key] = {"state": copy.deepcopy(original), "payload": {"action": SHORTAGE_PARENT}}
            cached = cache[key]
        state = copy.deepcopy(cached["state"])
        assert state["stash"] == original["stash"]
        assert "sibling" not in state
        state["sibling"] = question
        assert "sibling" not in cache[key]["state"]
    assert parent_calls == 1


def preflight(casebook: Path, *, expected_checksum: str = EXPECTED_CASEBOOK_CHECKSUM) -> dict[str, Any]:
    cases = _load(casebook)
    if len(cases) != 248:
        raise AssertionError(f"expected 248 cases, got {len(cases)}")
    ids = [str(case.get("golden_case_id") or "") for case in cases]
    expected_ids = [f"GFINAL-{index:04d}" for index in range(1, 249)]
    if ids != expected_ids:
        raise AssertionError("GFINAL IDs are not ordered GFINAL-0001..0248")
    checksum = _casebook_checksum(cases)
    if checksum != expected_checksum:
        raise AssertionError(f"casebook checksum drift: {checksum}")
    offline = audit(casebook, id_prefix="GFINAL", expected_count=248)
    if any(row["offline_result"] != "PASS" for row in offline):
        raise AssertionError("Golden offline audit is not clean")
    shortage = [case for case in cases if str(case.get("parent_question_raw") or "") == SHORTAGE_PARENT]
    if len(shortage) != 5 or {str(case["question_raw"]) for case in shortage} != SHORTAGE_QUESTIONS:
        raise AssertionError("shortage sibling population changed")
    _assert_sibling_restore_contract()
    if not _action_name_matches("계약단가 조회", "최종 계약단가 조회"):
        raise AssertionError("R070 screen/runtime compatibility missing")
    return {"cases": len(cases), "checksum": checksum, "shortage_siblings": len(shortage)}


def _direct_classification(case: dict[str, Any], payload: dict[str, Any] | None, handled: bool, error: str) -> str:
    actual = _payload_action(payload)
    status = str(_payload_meta(payload).get("result_status") or "")
    if error:
        return "EXECUTION_ERROR"
    if not handled or not actual:
        return "HARNESS_DEFECT"
    if not _action_name_matches(str(case["expected_action_raw"]), actual):
        return "ORACLE_REVIEW"
    if status == "input_required":
        return "EXPECTED_BLOCK" if case.get("execution_mode") in {"CLARIFICATION", "EXPECTED_BLOCK"} else "PRODUCTION_DEFECT"
    if status == "no_data":
        return "DATA_NO_RESULT_ALLOWED"
    return "PASS" if status in {"", "success"} else "EXECUTION_ERROR"


def _current_table_classification(detail: dict[str, Any]) -> str:
    if detail.get("verdict") == "PASS":
        return "PASS"
    status = str(detail.get("followup_status") or "")
    extra_calls = detail.get("current_table_extra_erp_source_call")
    if status == "no_data" and extra_calls == 0:
        return "DATA_NO_RESULT_ALLOWED"
    if status == "column_unavailable":
        return "EXPECTED_BLOCK"
    if detail.get("error") or not detail.get("followup_handled"):
        return "HARNESS_DEFECT"
    return "PRODUCTION_DEFECT"


def _performance_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        elapsed = int(row.get("elapsed_ms") or 0)
        if elapsed >= 10_000 or row.get("parent_question_raw") == SHORTAGE_PARENT:
            result.append({
                "golden_case_id": row["golden_case_id"], "source_seq": row["source_seq"],
                "question_raw": row["question_raw"], "parent_question_raw": row.get("parent_question_raw", ""),
                "elapsed_ms": elapsed, "over_10s": elapsed >= 10_000,
                "over_30s": elapsed >= 30_000, "over_60s": elapsed >= 60_000,
                "is_shortage_parent_family": row.get("parent_question_raw") == SHORTAGE_PARENT,
            })
    return result


def run(casebook: Path, output: Path, progress: Path, performance: Path) -> list[dict[str, Any]]:
    from app.sims.nlq import nlq_codes, nlq_goods, nlq_router, nlq_vendors
    from app.ui import chat_middleware, ssai_login

    cases = _load(casebook)
    original_company = get_current_company_id()
    original_st = chat_middleware.st
    original_push = chat_middleware.push_sims_result_to_chat
    rows: list[dict[str, Any]] = []
    parent_cache: dict[tuple[int, str], dict[str, Any]] = {}
    persistence = _RunPersistence(output, progress, rows)
    sequence = 0

    def next_seq() -> int:
        nonlocal sequence
        sequence += 1
        return sequence

    set_current_company_id(3)
    try:
        with patch.object(ssai_login, "get_selected_company", return_value={"company_id": 3}):
            for case in cases:
                persistence.start(case)
                started = time.perf_counter()
                parent_query = str(case.get("parent_question_raw") or "")
                parent_key = (3, parent_query)
                cached_parent = parent_cache.get(parent_key) if parent_query else None
                state = copy.deepcopy(cached_parent["state"]) if cached_parent else _new_state(str(case["golden_case_id"]))
                room = state["current_room"]
                chat_middleware.st = SimpleNamespace(session_state=state)
                capture = DeliveryCapture(original_push)
                error = ""
                try:
                    with (
                        patch.object(chat_middleware, "push_sims_result_to_chat", capture.push),
                        patch.object(nlq_goods, "push_sims_result_to_chat", capture.push),
                        patch.object(nlq_vendors, "push_sims_result_to_chat", capture.push),
                        patch.object(nlq_codes, "push_sims_result_to_chat", capture.push),
                    ):
                        if parent_query:
                            def remember_parent(handled, payload, parent_state):
                                parent_cache[parent_key] = {
                                    "handled": bool(handled), "payload": copy.deepcopy(payload),
                                    "state": copy.deepcopy(parent_state),
                                }

                            focused = FocusedCase(
                                str(case["golden_case_id"]), str(case["question_raw"]), str(case["expected_action_raw"]),
                                parent_question=parent_query, parent_action="", runtime_followup=str(case["question_raw"]),
                            )
                            detail = _run_current_table(
                                focused, nlq_router, state, room, capture, next_seq,
                                cached_parent=cached_parent,
                                parent_ready_hook=None if cached_parent else remember_parent,
                            )
                            classification = _current_table_classification(detail)
                            rows.append({
                                "golden_case_id": case["golden_case_id"], "source_seq": case["source_seq"],
                                "question_raw": case["question_raw"], "parent_question_raw": parent_query,
                                "expected_action_raw": case["expected_action_raw"], "execution_mode": case["execution_mode"],
                                "company_id": 3, "actual_action_raw": detail.get("followup_action", ""),
                                "parent_action_raw": detail.get("parent_action", ""), "source_action": detail.get("source_action", ""),
                                "result_status": detail.get("followup_status", ""), "row_count": detail.get("followup_rows", ""),
                                "source_table_key": detail.get("source_table_key", ""), "source_call_count": detail.get("parent_source_call_count", ""),
                                "parent_execution_count": 0 if cached_parent else 1, "parent_reused": bool(cached_parent),
                                "followup_extra_erp_calls": detail.get("current_table_extra_erp_source_call", ""),
                                "date_columns": detail.get("date_columns", ""), "valid_date_rows": detail.get("valid_date_rows", ""),
                                "delivered_columns": detail.get("result_columns", ""), "elapsed_ms": int((time.perf_counter() - started) * 1000),
                                "classification": classification, "note": "production parent/stash/pre-dispatch/follow-up", "error": detail.get("error", ""),
                            })
                        else:
                            handled = bool(nlq_router.try_handle_nlq(
                                str(case["question_raw"]), room=room, session_state=state,
                                make_ts=lambda: datetime.now().isoformat(timespec="seconds"), next_seq=next_seq, logger=LOG,
                            ))
                            payload = capture.items[-1] if capture.items else None
                            classification = _direct_classification(case, payload, handled, "")
                            rows.append({
                                "golden_case_id": case["golden_case_id"], "source_seq": case["source_seq"],
                                "question_raw": case["question_raw"], "parent_question_raw": "",
                                "expected_action_raw": case["expected_action_raw"], "execution_mode": case["execution_mode"],
                                "company_id": 3, "actual_action_raw": _payload_action(payload), "parent_action_raw": "", "source_action": "",
                                "result_status": _payload_meta(payload).get("result_status", ""), "row_count": _payload_rows(payload),
                                "source_table_key": "", "source_call_count": _payload_meta(payload).get("source_call_count", ""),
                                "parent_execution_count": 0, "parent_reused": False, "followup_extra_erp_calls": 0,
                                "date_columns": "", "valid_date_rows": "", "elapsed_ms": int((time.perf_counter() - started) * 1000),
                                **_delivery_audit(payload), "classification": classification,
                                "note": "production router and actual chat delivery boundary", "error": "",
                            })
                except BaseException as exc:
                    rows.append({
                        "golden_case_id": case["golden_case_id"], "source_seq": case["source_seq"],
                        "question_raw": case["question_raw"], "parent_question_raw": parent_query,
                        "expected_action_raw": case["expected_action_raw"], "execution_mode": case["execution_mode"], "company_id": 3,
                        "actual_action_raw": "", "parent_action_raw": "", "source_action": "", "result_status": "", "row_count": "",
                        "source_table_key": "", "source_call_count": "", "parent_execution_count": 0 if cached_parent else int(bool(parent_query)),
                        "parent_reused": bool(cached_parent), "followup_extra_erp_calls": "", "date_columns": "", "valid_date_rows": "",
                        "delivered_columns": "", "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        "classification": "EXECUTION_ERROR", "note": "unhandled production invocation exception",
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    })
                persistence.done(case)
    finally:
        chat_middleware.st = original_st
        chat_middleware.push_sims_result_to_chat = original_push
        set_current_company_id(original_company)
    persistence.complete()
    _write(performance, _performance_rows(rows))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--casebook", type=Path, required=True)
    parser.add_argument(
        "--expected-casebook-checksum",
        default=EXPECTED_CASEBOOK_CHECKSUM,
        help="Canonical checksum for the explicitly selected authoritative casebook.",
    )
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--company-id", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--progress", type=Path)
    parser.add_argument("--performance", type=Path)
    args = parser.parse_args()
    report = preflight(
        args.casebook.resolve(),
        expected_checksum=str(args.expected_casebook_checksum),
    )
    print(json.dumps({"preflight": "PASS", **report}, ensure_ascii=False))
    if args.preflight and not args.execute_live:
        return 0
    if not args.execute_live or args.company_id != 3 or not all((args.output, args.progress, args.performance)):
        raise SystemExit("live mode requires --execute-live --company-id 3 --output --progress --performance")
    rows = run(args.casebook.resolve(), args.output.resolve(), args.progress.resolve(), args.performance.resolve())
    print(json.dumps({"executed": len(rows), **dict(Counter(row["classification"] for row in rows))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run a bounded company-3 smoke against the exported Golden-250 casebook.

The tool reuses the production-faithful capture/stash helpers from the focused
regression harness.  It selects action families from Golden metadata rather
than case IDs, and does not implement a second router or ERP query.
"""

from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime
import io
import json
import logging
import os
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
from tools.check_nlq_official_206_regression import ACTION_COMPATIBILITY, _action_name_matches
from tools.check_nlq_regression_harness_alignment_20261006 import (
    DeliveryCapture, FocusedCase, _run_current_table,
)


TARGET_ACTIONS = (
    "SIMS 일일점검", "현재고 조회", "제품재고현황 조회", "제품수불현황 조회",
    "입고명세 조회", "출고명세 조회", "거래명세서 공통 조회", "그룹코드조회",
    "사용자조회", "거래처 목록", "최종 계약단가 조회", "최종 매입단가 조회",
    "발주조회", "입고예정조회", "제품정보 조회", "발주 계산",
)
SHORTAGE_CURRENT_TABLE_PARENT = "매입처별 재고부족현황"
SHORTAGE_CURRENT_TABLE_QUESTION = "현재표 부족제품수 top 10 보여줘"
LOG = logging.getLogger("nlq-golden-250-focused-smoke")


def _load(path: Path) -> list[dict[str, Any]]:
    return [dict(item) for item in json.loads(path.read_text(encoding="utf-8"))["cases"]]


def _payload_action(payload: dict[str, Any] | None) -> str:
    return str((payload or {}).get("action") or (payload or {}).get("title") or "").strip()


def _payload_meta(payload: dict[str, Any] | None) -> dict[str, Any]:
    return dict((payload or {}).get("meta") or {})


def _payload_rows(payload: dict[str, Any] | None) -> int | str:
    meta = _payload_meta(payload)
    for key in ("row_count_total", "row_count"):
        if meta.get(key) is not None:
            return meta[key]
    frame = (payload or {}).get("df")
    return len(frame) if hasattr(frame, "__len__") else ""


def _delivery_audit(payload: dict[str, Any] | None) -> dict[str, str]:
    """Persist delivery-contract shape without exporting business rows or values."""
    item = payload or {}
    frame = item.get("df")
    columns = [str(column) for column in frame.columns] if hasattr(frame, "columns") else []
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    meta = _payload_meta(item)
    condition = str(meta.get("condition") or meta.get("query_summary") or "")
    return {
        "delivered_columns": "|".join(columns),
        "params_keys": "|".join(sorted(str(key) for key in params)),
        "query_condition_present": "Y" if condition else "N",
    }


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Direct and Current Table deliveries expose different contract fields.
    # Preserve their union so a completed live run can always flush evidence.
    fields = list(dict.fromkeys(key for row in rows for key in row))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write(path, ("\ufeff" + buffer.getvalue()).encode("utf-8"))


def _write_progress(path: Path, *, state: str, rows: list[dict[str, Any]], current_case: dict[str, Any] | None = None) -> None:
    payload = {
        "state": state,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "completed_count": len(rows),
        "completed_case_ids": [str(row.get("golden_case_id") or "") for row in rows],
        "current_case_id": str((current_case or {}).get("golden_case_id") or ""),
        "current_source_seq": str((current_case or {}).get("source_seq") or ""),
    }
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


class _RunPersistence:
    """Durably preserve each explicit live invocation; no retry policy lives here."""

    def __init__(self, output: Path | None, progress: Path | None, rows: list[dict[str, Any]]) -> None:
        self.output = output
        self.progress = progress
        self.rows = rows

    def start(self, case: dict[str, Any]) -> None:
        if self.progress:
            _write_progress(self.progress, state="START", rows=self.rows, current_case=case)

    def done(self, case: dict[str, Any]) -> None:
        if self.output:
            _write(self.output, self.rows)
        if self.progress:
            _write_progress(self.progress, state="DONE", rows=self.rows, current_case=case)

    def complete(self) -> None:
        if self.output:
            _write(self.output, self.rows)
        if self.progress:
            _write_progress(self.progress, state="COMPLETE", rows=self.rows)


def _runtime_parent_action(parent_raw: str) -> str:
    candidates = ACTION_COMPATIBILITY.get(parent_raw)
    return sorted(candidates)[0] if candidates else parent_raw


def _select_shortage_current_table_protection(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Select the shortage ranking protection by its stable chat contract."""
    matches = [
        case
        for case in cases
        if str(case.get("parent_question_raw") or "").strip() == SHORTAGE_CURRENT_TABLE_PARENT
        and str(case.get("question_raw") or "").strip() == SHORTAGE_CURRENT_TABLE_QUESTION
    ]
    if len(matches) != 1:
        raise AssertionError(
            "Golden shortage Current Table protection must resolve to exactly one "
            f"semantic case; found={len(matches)}"
        )
    return matches[0]


def _direct_classification(case: dict[str, Any], payload: dict[str, Any] | None, handled: bool) -> str:
    actual = _payload_action(payload)
    status = str(_payload_meta(payload).get("result_status") or "")
    raw_expected = str(case["expected_action_raw"])
    if not handled or not actual:
        return "HARNESS_DEFECT"
    if not _action_name_matches(raw_expected, actual):
        # The Golden raw action has no direct current registry action in this
        # family.  Do not silently substitute a neighboring IO action.
        return "SOURCE_REVIEW" if raw_expected == "지역별 매출현황" else "ORACLE_REVIEW"
    if status == "input_required":
        return "HARNESS_DEFECT"
    if status == "no_data":
        return "DATA_NO_RESULT"
    return "PASS" if status in {"", "success"} else "HARNESS_DEFECT"


def _current_table_classification(detail: dict[str, Any]) -> str:
    if detail.get("verdict") == "PASS":
        return "PASS"
    if (
        detail.get("followup_status") == "no_data"
        and detail.get("date_columns")
        and detail.get("valid_date_rows") == 0
        and detail.get("source_rows", 0) > 0
        and detail.get("current_table_extra_erp_source_call") == 0
    ):
        return "DATA_NO_RESULT_ALLOWED"
    return "PRODUCTION_DEFECT_CANDIDATE"


def run(
    casebook: Path,
    *,
    source_seqs: tuple[str, ...] = (),
    output: Path | None = None,
    progress: Path | None = None,
) -> list[dict[str, Any]]:
    from app.sims.nlq import nlq_codes, nlq_goods, nlq_router, nlq_vendors
    from app.ui import chat_middleware, ssai_login

    cases = _load(casebook)
    by_action = {action: next((case for case in cases if not case["parent_question_raw"] and case["resolved_expected_action"] == action), None) for action in TARGET_ACTIONS}
    selected = [case for case in by_action.values() if case]
    selected.append(_select_shortage_current_table_protection(cases))
    if source_seqs:
        selected_by_seq = {case["source_seq"]: case for case in cases}
        selected = [selected_by_seq[seq] for seq in source_seqs]
    if not selected:
        raise AssertionError("Golden focused action selection incomplete")

    original_company = get_current_company_id()
    original_st = chat_middleware.st
    original_push = chat_middleware.push_sims_result_to_chat
    results: list[dict[str, Any]] = []
    parent_cache: dict[tuple[int, str], dict[str, Any]] = {}
    persistence = _RunPersistence(output, progress, results)
    set_current_company_id(3)
    try:
        for case in selected:
            persistence.start(case)
            parent_key = (3, str(case.get("parent_question_raw") or ""))
            cached_parent = parent_cache.get(parent_key) if parent_key[1] else None
            state: dict[str, Any] = copy.deepcopy(cached_parent["state"]) if cached_parent else {
                "current_room": {"id": f"golden-{case['golden_case_id']}", "company": "3", "messages": []},
                "chat_rooms": [], "sims_tables": {}, "sims_export_tables": {},
                "__sims_export_tables_by_key": {}, "__io_pending_product_pick": {},
            }
            room = state["current_room"]
            capture = DeliveryCapture(chat_middleware.wssz)
            sequence = 0
            def next_seq() -> int:
                nonlocal sequence
                sequence += 1
                return sequence
            chat_middleware.st = SimpleNamespace(session_state=state)
            started = time.perf_counter()
            error = ""
            before = len(capture.items)
            try:
                with (
                    patch.object(chat_middleware, "push_sims_result_to_chat", capture.push),
                    patch.object(nlq_goods, "push_sims_result_to_chat", capture.push),
                    patch.object(nlq_vendors, "push_sims_result_to_chat", capture.push),
                    patch.object(nlq_codes, "push_sims_result_to_chat", capture.push),
                    patch.object(ssai_login, "get_selected_company", return_value={"company_id": 3}),
                ):
                    if case["parent_question_raw"]:
                        def remember_parent(handled, payload, parent_state):
                            parent_cache[parent_key] = {
                                "handled": handled, "payload": copy.deepcopy(payload),
                                "state": copy.deepcopy(parent_state),
                            }

                        focused = FocusedCase(
                            case["golden_case_id"], case["question_raw"], case["expected_action_raw"],
                            parent_question=case["parent_question_raw"],
                            parent_action="",
                            runtime_followup=case["question_raw"],
                        )
                        detail = _run_current_table(
                            focused, nlq_router, state, room, capture, next_seq,
                            cached_parent=cached_parent,
                            parent_ready_hook=None if cached_parent else remember_parent,
                        )
                        classification = _current_table_classification(detail)
                        results.append({
                            "golden_case_id": case["golden_case_id"], "source_seq": case["source_seq"],
                            "question_raw": case["question_raw"], "parent_question_raw": case["parent_question_raw"],
                            "expected_action_raw": case["expected_action_raw"],
                            "execution_mode": "CURRENT_TABLE_DETERMINISTIC", "company_id": 3,
                            "actual_action_raw": detail.get("followup_action", ""), "result_status": detail.get("followup_status", ""),
                            "row_count": detail.get("followup_rows", ""), "source_table_key": detail.get("source_table_key", ""),
                            "source_action": detail.get("source_action", ""),
                            "parent_action": detail.get("parent_action", ""),
                            "parent_reused": detail.get("parent_reused", False),
                            "source_rows": detail.get("source_rows", ""),
                            "date_columns": detail.get("date_columns", ""),
                            "valid_date_rows": detail.get("valid_date_rows", ""),
                            "source_call_count": detail.get("parent_source_call_count", ""),
                            "followup_extra_erp_calls": detail.get("current_table_extra_erp_source_call", ""),
                            "elapsed_ms": detail.get("elapsed_ms", 0),
                            "delivered_columns": detail.get("result_columns", ""),
                            "params_keys": "", "query_condition_present": "",
                            "classification": classification,
                            "result": classification,
                            "note": "production parent/stash/pre-dispatch/follow-up", "error": detail.get("error", ""),
                        })
                        persistence.done(case)
                        continue
                    handled = bool(nlq_router.try_handle_nlq(
                        case["question_raw"], room=room, session_state=state,
                        make_ts=lambda: datetime.now().isoformat(timespec="seconds"), next_seq=next_seq, logger=LOG,
                    ))
                    payload = capture.items[-1] if len(capture.items) > before else None
            except BaseException as exc:
                handled = False
                payload = None
                error = f"{type(exc).__name__}: {exc}"[:300]
                if case["parent_question_raw"]:
                    results.append({
                        "golden_case_id": case["golden_case_id"], "source_seq": case["source_seq"],
                        "question_raw": case["question_raw"], "parent_question_raw": case["parent_question_raw"],
                        "expected_action_raw": case["expected_action_raw"],
                        "execution_mode": "CURRENT_TABLE_DETERMINISTIC", "company_id": 3,
                        "actual_action_raw": "", "result_status": "", "row_count": "",
                        "source_table_key": "", "source_action": "", "source_call_count": "",
                        "followup_extra_erp_calls": "",
                        "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        "delivered_columns": "", "params_keys": "", "query_condition_present": "",
                        "classification": "HARNESS_DEFECT", "result": "HARNESS_DEFECT",
                        "note": "production parent/stash/pre-dispatch/follow-up", "error": error,
                    })
                    persistence.done(case)
                    continue
            actual = _payload_action(payload)
            meta = _payload_meta(payload)
            delivery_audit = _delivery_audit(payload)
            classification = _direct_classification(case, payload, handled)
            results.append({
                "golden_case_id": case["golden_case_id"], "source_seq": case["source_seq"],
                "question_raw": case["question_raw"], "parent_question_raw": "", "execution_mode": case["execution_mode"],
                "expected_action_raw": case["expected_action_raw"],
                "company_id": 3, "actual_action_raw": actual, "result_status": meta.get("result_status", ""),
                "row_count": _payload_rows(payload), "source_table_key": "", "source_action": "",
                "source_call_count": meta.get("source_call_count", ""),
                "followup_extra_erp_calls": 0, "elapsed_ms": int((time.perf_counter() - started) * 1000),
                **delivery_audit,
                "classification": classification, "result": classification,
                "note": "production router and actual chat delivery boundary", "error": error,
            })
            persistence.done(case)
    finally:
        chat_middleware.st = original_st
        chat_middleware.push_sims_result_to_chat = original_push
        set_current_company_id(original_company)
    persistence.complete()
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--casebook", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--source-seq", action="append")
    parser.add_argument("--progress", type=Path)
    args = parser.parse_args()
    if args.company_id != 3 or not args.execute_live:
        raise SystemExit("company-id must be 3 and --execute-live is required")
    rows = run(
        args.casebook.resolve(),
        source_seqs=tuple(args.source_seq or ()),
        output=args.output.resolve(),
        progress=args.progress.resolve() if args.progress else None,
    )
    print(json.dumps({"executed": len(rows), **dict(__import__("collections").Counter(row["result"] for row in rows))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

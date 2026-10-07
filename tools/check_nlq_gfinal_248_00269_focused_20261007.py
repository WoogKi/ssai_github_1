"""Company-3 positive evidence for the authoritative 00269 stock-ledger parent.

This is intentionally a focused production-faithful runner.  It invokes its
parent once, captures the actual chat stash, then deep-copies that untouched
state for each current-table sibling.  It never searches for another product
and has no retry path.
"""

from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.mssql_client import get_current_company_id, set_current_company_id
from app.ui.current_table_followups.time_grouping import derive_current_table_time_grouping
from tools.check_nlq_gfinal_248_full_live_20261007 import _new_state
from tools.check_nlq_golden_250_focused_smoke_20261007 import DeliveryCapture, _RunPersistence
from tools.check_nlq_regression_harness_alignment_20261006 import (
    FocusedCase,
    _payload_action,
    _payload_meta,
    _run_current_table,
    _source_call_count,
)


PARENT_QUERY = "제품수불현황 제품 00269 2024~2026 조회"
FOLLOWUPS = (
    "현재표 일별 집계",
    "현재표 월별 집계",
    "현재표 요일별 집계",
)
DATE_COLUMN = "입출고일자"


def _frame_from_state(state: dict[str, Any]) -> tuple[str, str, pd.DataFrame | None]:
    key = str(state.get("__sims_current_table_source_key") or "")
    action = str(state.get("__sims_current_table_source_action") or "")
    frames = [
        store.get(key)
        for name in ("__sims_export_tables_by_key", "sims_export_tables", "sims_tables")
        for store in (state.get(name),)
        if key and isinstance(store, dict) and isinstance(store.get(key), pd.DataFrame)
    ]
    return key, action, max(frames, key=len) if frames else None


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        __import__("os").fsync(handle.fileno())


def run(output: Path, progress: Path) -> list[dict[str, Any]]:
    from app.sims.nlq import nlq_codes, nlq_goods, nlq_router, nlq_vendors
    from app.ui import chat_middleware, ssai_login

    original_company = get_current_company_id()
    original_st = chat_middleware.st
    original_push = chat_middleware.push_sims_result_to_chat
    rows: list[dict[str, Any]] = []
    persistence = _RunPersistence(output, progress, rows)
    state = _new_state("00269-focused")
    room = state["current_room"]
    sequence = 0

    def next_seq() -> int:
        nonlocal sequence
        sequence += 1
        return sequence

    set_current_company_id(3)
    try:
        with patch.object(ssai_login, "get_selected_company", return_value={"company_id": 3}):
            chat_middleware.st = SimpleNamespace(session_state=state)
            capture = DeliveryCapture(original_push)
            parent_case = {"golden_case_id": "GFINAL-0090", "source_seq": "90"}
            persistence.start(parent_case)
            with (
                patch.object(chat_middleware, "push_sims_result_to_chat", capture.push),
                patch.object(nlq_goods, "push_sims_result_to_chat", capture.push),
                patch.object(nlq_vendors, "push_sims_result_to_chat", capture.push),
                patch.object(nlq_codes, "push_sims_result_to_chat", capture.push),
            ):
                before = len(capture.items)
                handled = bool(nlq_router.try_handle_nlq(
                    PARENT_QUERY, room=room, session_state=state,
                    make_ts=lambda: datetime.now().isoformat(timespec="seconds"),
                    next_seq=next_seq, logger=__import__("logging").getLogger(__name__),
                ))
                parent_payload = capture.items[-1] if len(capture.items) > before else None
                source_key, source_action, frame = _frame_from_state(state)
                valid_dates = 0
                if isinstance(frame, pd.DataFrame) and DATE_COLUMN in frame.columns:
                    valid_dates = int(derive_current_table_time_grouping(frame[DATE_COLUMN], "day").ne("").sum())
                parent_status = str(_payload_meta(parent_payload).get("result_status") or "")
                parent_row = {
                    "golden_case_id": "GFINAL-0090", "source_seq": "90", "kind": "parent",
                    "question_raw": PARENT_QUERY, "company_id": 3, "parent_execution_count": 1,
                    "parent_reused": False, "parent_handled": handled,
                    "actual_action_raw": _payload_action(parent_payload), "result_status": parent_status,
                    "source_rows": len(frame) if isinstance(frame, pd.DataFrame) else 0,
                    "source_table_key_present": bool(source_key), "source_action": source_action,
                    "date_column_present": bool(isinstance(frame, pd.DataFrame) and DATE_COLUMN in frame.columns),
                    "valid_date_rows": valid_dates, "source_call_count": _source_call_count(parent_payload),
                    "followup_extra_erp_calls": 0,
                    "classification": "PASS" if handled and parent_status == "success" and valid_dates > 0 else "DATA_NO_RESULT_ALLOWED",
                    "note": "single authoritative parent invocation; no product discovery",
                }
                rows.append(parent_row)
                persistence.done(parent_case)
                original_parent = {"handled": handled, "payload": copy.deepcopy(parent_payload), "state": copy.deepcopy(state)}

                if not (handled and parent_status == "success" and isinstance(frame, pd.DataFrame) and valid_dates > 0):
                    persistence.complete()
                    return rows

                for offset, question in enumerate(FOLLOWUPS, start=1):
                    case_id = f"GFINAL-{90 + offset:04d}"
                    case_meta = {"golden_case_id": case_id, "source_seq": str(90 + offset)}
                    persistence.start(case_meta)
                    sibling_state = copy.deepcopy(original_parent["state"])
                    chat_middleware.st = SimpleNamespace(session_state=sibling_state)
                    detail = _run_current_table(
                        FocusedCase(case_id, question, "제품수불부", parent_question=PARENT_QUERY, runtime_followup=question),
                        nlq_router, sibling_state, sibling_state["current_room"], capture, next_seq,
                        cached_parent=original_parent,
                    )
                    classification = "PASS" if detail.get("verdict") == "PASS" and detail.get("current_table_extra_erp_source_call") == 0 else "PRODUCTION_DEFECT"
                    rows.append({
                        "golden_case_id": case_id, "source_seq": str(90 + offset), "kind": "followup",
                        "question_raw": question, "company_id": 3, "parent_execution_count": 0,
                        "parent_reused": True, "actual_action_raw": detail.get("followup_action", ""),
                        "result_status": detail.get("followup_status", ""), "source_rows": detail.get("source_rows", ""),
                        "source_table_key_present": bool(detail.get("source_table_key")), "source_action": detail.get("source_action", ""),
                        "date_column_present": DATE_COLUMN in str(detail.get("date_columns") or "").split("|"),
                        "valid_date_rows": detail.get("valid_date_rows", ""), "source_call_count": 0,
                        "followup_extra_erp_calls": detail.get("current_table_extra_erp_source_call", ""),
                        "required_date_result": "요일" if "요일" in question else ("월" if "월" in question else "일자"),
                        "result_columns": detail.get("result_columns", ""), "classification": classification,
                        "note": "deep-copied original parent stash/provenance", "error": detail.get("error", ""),
                    })
                    persistence.done(case_meta)
    finally:
        chat_middleware.st = original_st
        chat_middleware.push_sims_result_to_chat = original_push
        set_current_company_id(original_company)
    persistence.complete()
    _write_rows(output, rows)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-id", type=int, required=True)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    args = parser.parse_args()
    if args.company_id != 3 or not args.execute_live:
        raise SystemExit("company-id must be 3 and --execute-live is required")
    rows = run(args.output.resolve(), args.progress.resolve())
    print(json.dumps({"rows": len(rows), "classification": [row["classification"] for row in rows]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
